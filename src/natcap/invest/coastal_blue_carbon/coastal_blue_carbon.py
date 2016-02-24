# -*- coding: utf-8 -*-
"""Coastal Blue Carbon Model."""

import csv
import os
import pprint as pp
import shutil
import logging
import math
import time
import itertools

import numpy as np
from osgeo import gdal
import pygeoprocessing.geoprocessing as geoprocess

from . import io
from .. import utils as invest_utils

# using largest negative 32-bit floating point number
# reasons: practical limit for 32 bit floating point and most outputs should
#          be positive
NODATA_FLOAT = -16777216

logging.basicConfig(format='%(asctime)s %(name)-20s %(levelname)-8s \
%(message)s', level=logging.DEBUG, datefmt='%m/%d/%Y %H:%M:%S ')
LOGGER = logging.getLogger(
    'natcap.invest.coastal_blue_carbon.coastal_blue_carbon')


def execute(args):
    """Entry point for Coastal Blue Carbon model.

    Args:
        workspace_dir (str): location into which all intermediate and
            output files should be placed.
        results_suffix (str): a string to append to output filenames.
        lulc_lookup_uri (str): filepath to a CSV table used to convert
            the lulc code to a name. Also used to determine if a given lulc
            type is a coastal blue carbon habitat.
        lulc_transition_matrix_uri (str): generated by the preprocessor. This
            file must be edited before it can be used by the main model. The
            left-most column represents the source lulc class, and the top row
            represents the destination lulc class.
        carbon_pool_initial_uri (str): the provided CSV table contains
            information related to the initial conditions of the carbon stock
            within each of the three pools of a habitat. Biomass includes
            carbon stored above and below ground. All non-coastal blue carbon
            habitat lulc classes are assumed to contain no carbon. The values
            for 'biomass', 'soil', and 'litter' should be given in terms of
            Megatonnes CO2 e/ ha.
        carbon_pool_transient_uri (str): the provided CSV table contains
            information related to the transition of carbon into and out of
            coastal blue carbon pools. All non-coastal blue carbon habitat lulc
            classes are assumed to neither sequester nor emit carbon as a
            result of change. The 'yearly_accumulation' values should be given
            in terms of Megatonnes of CO2 e/ha-yr. The 'half-life' values must
            be given in terms of years. The 'disturbance' values must be given
            as a decimal (e.g. 0.5 for 50%) of stock distrubed given a transition
            occurs away from a lulc-class.
        lulc_baseline_map_uri (str): a GDAL-supported raster representing the
            baseline landscape/seascape.
        lulc_transition_maps_list (list): a list of GDAL-supported rasters
            representing the landscape/seascape at particular points in time.
            Provided in chronological order.
        lulc_transition_years_list (list): a list of years that respectively
            correspond to transition years of the rasters. Provided in
            chronological order.
        analysis_year (int): optional. Indicates how many timesteps to run the
            transient analysis beyond the last transition year. Must come
            chronologically after the last transition year if provided.
            Otherwise, the final timestep of the model will be set to the last
            transition year.
        do_economic_analysis (bool): boolean value indicating whether model
            should run economic analysis.
        do_price_table (bool): boolean value indicating whether a price table
            is included in the arguments and to be used or a price and interest
            rate is provided and to be used instead.
        price (float): the price per Megatonne CO2 e at the base year.
        interest_rate (float): the interest rate on the price per Megatonne
            CO2e, compounded yearly.  Provided as a percentage (e.g. 3.0 for
            3%).
        price_table_uri (bool): if `args['do_price_table']` is set to `True`
            the provided CSV table is used in place of the initial price and
            interest rate inputs. The table contains the price per Megatonne
            CO2e sequestered for a given year, for all years from the original
            snapshot to the analysis year, if provided.
        discount_rate (float): the discount rate on future valuations of
            sequestered carbon, compounded yearly.  Provided as a percentage
            (e.g. 3.0 for 3%).

    Example Args::

        args = {
            'workspace_dir': 'path/to/workspace/',
            'results_suffix': '',
            'lulc_lookup_uri': 'path/to/lulc_lookup_uri',
            'lulc_transition_matrix_uri': 'path/to/lulc_transition_uri',
            'carbon_pool_initial_uri': 'path/to/carbon_pool_initial_uri',
            'carbon_pool_transient_uri': 'path/to/carbon_pool_transient_uri',
            'lulc_baseline_map_uri': 'path/to/baseline_map.tif',
            'lulc_transition_maps_list': [raster1_uri, raster2_uri, ...],
            'lulc_transition_years_list': [2000, 2005, ...],
            'analysis_year': 2100,
            'do_economic_analysis': '<boolean>',
            'do_price_table': '<boolean>',
            'price': '<float>',
            'interest_rate': '<float>',
            'price_table_uri': 'path/to/price_table',
            'discount_rate': '<float>'
        }
    """
    LOGGER.info("Starting Coastal Blue Carbon model run...")
    d = io.get_inputs(args)

    # Setup Logging
    num_blocks = get_num_blocks(d['C_prior_raster'])
    current_time = time.time()

    block_iterator = enumerate(geoprocess.iterblocks(d['C_prior_raster']))
    C_nodata = geoprocess.get_nodata_from_uri(d['C_prior_raster'])

    for block_idx, (offset_dict, C_prior) in block_iterator:
        # Update User
        if time.time() - current_time >= 2.0:
            LOGGER.info("Processing block %i of %i" %
                        (block_idx+1, num_blocks))
            current_time = time.time()

        # Initialization
        timesteps = d['timesteps']

        x_size, y_size = C_prior.shape

        # timesteps+1 to include initial conditions
        stock_shape = (timesteps+1, x_size, y_size)
        S_biomass = np.zeros(stock_shape, dtype=np.float32)
        S_soil = np.zeros(stock_shape, dtype=np.float32)
        S_litter = np.zeros(stock_shape, dtype=np.float32)
        T = np.zeros(stock_shape, dtype=np.float32)

        timestep_shape = (timesteps, x_size, y_size)
        A_biomass = np.zeros(timestep_shape, dtype=np.float32)
        A_soil = np.zeros(timestep_shape, dtype=np.float32)
        E_biomass = np.zeros(timestep_shape, dtype=np.float32)
        E_soil = np.zeros(timestep_shape, dtype=np.float32)
        N_biomass = np.zeros(timestep_shape, dtype=np.float32)
        N_soil = np.zeros(timestep_shape, dtype=np.float32)
        V = np.zeros(timestep_shape, dtype=np.float32)
        P = np.zeros(timestep_shape, dtype=np.float32)

        transition_shape = (d['transitions'], x_size, y_size)
        L = np.zeros(transition_shape, dtype=np.float32)
        Y_biomass = np.zeros(transition_shape, dtype=np.float32)
        Y_soil = np.zeros(transition_shape, dtype=np.float32)
        D_biomass = np.zeros(transition_shape, dtype=np.float32)
        D_soil = np.zeros(transition_shape, dtype=np.float32)
        H_biomass = np.zeros(transition_shape, dtype=np.float32)
        H_soil = np.zeros(transition_shape, dtype=np.float32)
        R_biomass = np.zeros(transition_shape, dtype=np.float32)
        R_soil = np.zeros(transition_shape, dtype=np.float32)

        # Set Accum and Disturbance Values
        C_r = [read_from_raster(i, offset_dict) for i in d['C_r_rasters']]
        C_list = [C_prior] + C_r
        for i in xrange(0, d['transitions']):
            D_biomass[i] = reclass_transition(
                C_list[i],
                C_list[i+1],
                d['lulc_trans_to_Db'],
                out_dtype=np.float32,
                nodata_mask=C_nodata)
            D_soil[i] = reclass_transition(
                C_list[i],
                C_list[i+1],
                d['lulc_trans_to_Ds'],
                out_dtype=np.float32,
                nodata_mask=C_nodata)
            H_biomass[i] = reclass(
                C_list[i],
                d['lulc_to_Hb'],
                out_dtype=np.float32,
                nodata_mask=C_nodata)
            H_soil[i] = reclass(
                C_list[i], d['lulc_to_Hs'],
                out_dtype=np.float32,
                nodata_mask=C_nodata)
            L[i] = reclass(
                C_r[i],
                d['lulc_to_L'],
                out_dtype=np.float32,
                nodata_mask=C_nodata)
            Y_biomass[i] = reclass(
                C_r[i], d['lulc_to_Yb'],
                out_dtype=np.float32,
                nodata_mask=C_nodata)
            Y_soil[i] = reclass(
                C_r[i],
                d['lulc_to_Ys'],
                out_dtype=np.float32,
                nodata_mask=C_nodata)

        S_biomass[0] = reclass(
            C_prior,
            d['lulc_to_Sb'],
            out_dtype=np.float32,
            nodata_mask=C_nodata)
        S_soil[0] = reclass(
            C_prior,
            d['lulc_to_Ss'],
            out_dtype=np.float32,
            nodata_mask=C_nodata)
        S_litter[0] = reclass(
            C_prior,
            d['lulc_to_L'],
            out_dtype=np.float32,
            nodata_mask=C_nodata)
        T[0] = S_biomass[0] + S_soil[0] + S_litter[0]

        R_biomass[0] = D_biomass[0] * S_biomass[0]
        R_soil[0] = D_soil[0] * S_soil[0]

        # Transient Analysis
        for i in xrange(0, timesteps):
            transition_idx = timestep_to_transition_idx(
                d['snapshot_years'], d['transitions'], i)

            if is_transition_year(d['snapshot_years'], d['transitions'], i):
                # Set disturbed stock values
                R_biomass[transition_idx] = \
                    D_biomass[transition_idx] * S_biomass[i]
                R_soil[transition_idx] = D_soil[transition_idx] * S_soil[i]

            # Accumulation
            A_biomass[i] = Y_biomass[transition_idx]
            A_soil[i] = Y_soil[transition_idx]

            # Emissions
            E_biomass[i] = np.zeros(A_biomass[0].shape)
            E_soil[i] = np.zeros(A_biomass[0].shape)
            for transition_idx in xrange(0, timestep_to_transition_idx(
                    d['snapshot_years'], d['transitions'], i)+1):
                j = d['transition_years'][transition_idx] - \
                        d['transition_years'][0]
                E_biomass[i] += R_biomass[transition_idx] * \
                    (0.5**(i-j) - 0.5**(i-j+1))
                E_soil[i] += R_soil[transition_idx] * \
                    (0.5**(i-j) - 0.5**(i-j+1))

            # Net Sequestration
            N_biomass[i] = A_biomass[i] - E_biomass[i]
            N_soil[i] = A_soil[i] - E_soil[i]

            # Next Stock
            S_biomass[i+1] = S_biomass[i] + N_biomass[i]
            S_soil[i+1] = S_soil[i] + N_soil[i]
            T[i+1] = S_biomass[i+1] + S_soil[i+1] + S_litter[i+1]

            # Net Present Value
            if d['do_economic_analysis']:
                V[i] = (N_biomass[i] + N_soil[0]) * d['price_t'][i]

        # Write outputs: T_s, A_r, E_r, N_r, NPV
        s_years = d['snapshot_years']
        num_snapshots = len(s_years)

        A = A_biomass + A_soil
        E = E_biomass + E_soil
        N = N_biomass + N_soil

        A_r = [sum(A[s_to_timestep(s_years, i):s_to_timestep(s_years, i+1)])
               for i in xrange(0, num_snapshots-1)]
        E_r = [sum(E[s_to_timestep(s_years, i):s_to_timestep(s_years, i+1)])
               for i in xrange(0, num_snapshots-1)]
        N_r = [sum(N[s_to_timestep(s_years, i):s_to_timestep(s_years, i+1)])
               for i in xrange(0, num_snapshots-1)]

        T_s = [T[s_to_timestep(s_years, i)] for i in xrange(0, num_snapshots)]
        N_total = sum(N)

        raster_tuples = [
            ('T_s_rasters', T_s),
            ('A_r_rasters', A_r),
            ('E_r_rasters', E_r),
            ('N_r_rasters', N_r)]

        for key, array in raster_tuples:
            write_rasters(d['File_Registry'][key], array, offset_dict)

        write_to_raster(
            d['File_Registry']['N_total_raster'],
            N_total,
            offset_dict['xoff'],
            offset_dict['yoff'])

        if d['do_economic_analysis']:
            NPV = np.sum(V, axis=0)
            write_to_raster(
                d['File_Registry']['NPV_raster'],
                NPV,
                offset_dict['xoff'],
                offset_dict['yoff'])

    LOGGER.info("...Coastal Blue Carbon model run complete.")


def timestep_to_transition_idx(snapshot_years, transitions, timestep):
    """Convert timestep to transition index.

    Args:
        snapshot_years (list): a list of years corresponding to the provided
            rasters
        transitions (int): the number of transitions in the scenario
        timestep (int): the current timestep

    Returns:
        transition_idx (int): the current transition
    """
    for i in xrange(0, transitions):
        if timestep < (snapshot_years[i+1] - snapshot_years[0]):
            return i


def s_to_timestep(snapshot_years, snapshot_idx):
    """Convert snapshot index position to timestep.

    Args:
        snapshot_years (list): list of snapshot years.
        snapshot_idx (int): index of snapshot

    Returns:
        snapshot_timestep (int): timestep of the snapshot
    """
    return snapshot_years[snapshot_idx] - snapshot_years[0]


def is_transition_year(snapshot_years, transitions, timestep):
    """Check whether given timestep is a transition year.

    Args:
        snapshot_years (list): list of snapshot years.
        transitions (int): number of transitions.
        timestep (int): current timestep.

    Returns:
        is_transition_year (bool): whether the year corresponding to the
            timestep is a transition year.
    """
    if (timestep_to_transition_idx(snapshot_years, transitions, timestep) !=
        timestep_to_transition_idx(snapshot_years, transitions, timestep-1) and
            timestep_to_transition_idx(snapshot_years, transitions, timestep)):
        return True
    return False


def get_num_blocks(raster_uri):
    """Get the number of blocks in a raster file.

    Args:
        raster_uri (str): filepath to raster

    Returns:
        num_blocks (int): number of blocks in raster
    """
    ds = gdal.Open(raster_uri)
    n_rows = ds.RasterYSize
    n_cols = ds.RasterXSize

    band = ds.GetRasterBand(1)
    cols_per_block, rows_per_block = band.GetBlockSize()

    n_col_blocks = int(math.ceil(n_cols / float(cols_per_block)))
    n_row_blocks = int(math.ceil(n_rows / float(rows_per_block)))

    ds.FlushCache()
    ds = None

    return n_col_blocks * n_row_blocks


def reclass(array, d, nodata=None, out_dtype=None, nodata_mask=None):
    """Reclassify values in array.

    If a nodata value is not provided, the function will return an array with
    NaN values in its place to mark cells that could not be reclassed.

    Args:
        array (np.array): input data
        d (dict): reclassification map

    Returns:
        reclass_array (np.array): reclassified array
    """
    if out_dtype:
        array = array.astype(out_dtype)
    u = np.unique(array)
    has_map = np.in1d(u, d.keys())
    if np.issubdtype(array.dtype, int):
        ndata = np.iinfo(array.dtype).min
    else:
        ndata = np.finfo(array.dtype).min

    if not all(has_map):
        LOGGER.info("No value provided for the following codes %s" %
                    (str(u[~has_map])))

    a_ravel = array.ravel()
    a_ravel[~has_map] = ndata
    d[ndata] = ndata
    k = sorted(d.keys())
    v = np.array([d[key] for key in k])
    index = np.digitize(a_ravel, k, right=True)
    reclass_array = v[index].reshape(array.shape)

    if nodata_mask and np.issubdtype(reclass_array.dtype, float):
        reclass_array[array == nodata_mask] = np.nan

    return reclass_array


def reclass_transition(a_prev, a_next, trans_dict, out_dtype=None, nodata_mask=None):
    """Reclass arrays based on element-wise combinations between two arrays.

    Args:
        a_prev (np.array): previous lulc array
        a_next (np.array): next lulc array
        trans_dict (dict): reclassification map

    Returns:
        reclass_array (np.array): reclassified array
    """
    a = a_prev.flatten()
    b = a_next.flatten()
    c = np.ma.masked_array(np.zeros(a.shape))
    if out_dtype:
        c = c.astype(out_dtype)

    z = enumerate(itertools.izip(a, b))
    for index, transition_tuple in z:
        if transition_tuple in trans_dict:
            c[index] = trans_dict[transition_tuple]
        else:
            c[index] = np.ma.masked

    if nodata_mask and np.issubdtype(c.dtype, float):
        c[a == nodata_mask] = np.nan

    return c.reshape(a_prev.shape)


def write_to_raster(output_raster, array, xoff, yoff):
    """Write numpy array to raster block.

    Args:
        output_raster (str): filepath to output raster
        array (np.array): block to save to raster
        xoff (int): offset index for x-dimension
        yoff (int): offset index for y-dimension
    """
    ds = gdal.Open(output_raster, gdal.GA_Update)
    band = ds.GetRasterBand(1)
    if np.issubdtype(array.dtype, float):
        array[array == np.nan] = NODATA_FLOAT
    band.WriteArray(array, xoff, yoff)
    ds = None


def read_from_raster(input_raster, offset_block):
    """Read numpy array from raster block.

    Args:
        input_raster (str): filepath to input raster
        offset_block (dict): dictionary of offset information

    Returns:
        array (np.array): a blocked array of the input raster
    """
    ds = gdal.Open(input_raster)
    band = ds.GetRasterBand(1)
    array = band.ReadAsArray(**offset_block)
    ds = None
    return array


def write_rasters(raster_list, array_list, offset_dict):
    """Write rasters.

    Args:
        raster_list (list): list of output raster filepaths
        array_list (np.array): arrays to write to raster
        offset_dict (dict): information for where to write arrays to rasters
    """
    for i in xrange(0, len(raster_list)):
        write_to_raster(
            raster_list[i],
            array_list[i],
            offset_dict['xoff'],
            offset_dict['yoff'])
