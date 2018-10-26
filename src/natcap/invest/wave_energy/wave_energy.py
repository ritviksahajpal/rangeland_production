"""InVEST Wave Energy Model Core Code"""
from __future__ import absolute_import

import math
import os
import logging
import struct
import itertools
import shutil
import tempfile

import numpy
import pandas
import scipy
from osgeo import gdal
from osgeo import osr
from osgeo import ogr

import pygeoprocessing
from .. import validation
from .. import utils

import pdb
LOGGER = logging.getLogger('natcap.invest.wave_energy.wave_energy')

# Set nodata value and target_pixel_type for new rasters
_NODATA = float(numpy.finfo(numpy.float32).min) + 1.0
_TARGET_PIXEL_TYPE = gdal.GDT_Float32

# The life span (25 years) of a wave energy conversion facility.
_LIFE_SPAN = 25


class IntersectionError(Exception):
    """A custom error message for when the AOI does not intersect any wave
        data points.
    """
    pass


def execute(args):
    """Wave Energy.

    Executes both the biophysical and valuation parts of the wave energy model
    (WEM). Files will be written on disk to the intermediate and output
    directories. The outputs computed for biophysical and valuation include:
    wave energy capacity raster, wave power raster, net present value raster,
    percentile rasters for the previous three, and a point shapefile of the
    wave points with attributes.

    Args:
        workspace_dir (str): Where the intermediate and output folder/files
            will be saved. (required)
        wave_base_data_path (str): Directory location of wave base data
            including WAVEWATCH III (WW3) data and analysis area shapefile.
            (required)
        analysis_area_path (str): A string identifying the analysis area of
            interest. Used to determine wave data shapefile, wave data text
            file, and analysis area boundary shape. (required)
        aoi_path (str): A polygon shapefile outlining a more detailed area
            within the analysis area. This shapefile should be projected with
            linear units being in meters. (required to run Valuation model)
        machine_perf_path (str): The path of a CSV file that holds the
            machine performance table. (required)
        machine_param_path (str): The path of a CSV file that holds the
            machine parameter table. (required)
        dem_path (str): The path of the Global Digital Elevation Model (DEM).
            (required)
        suffix (str): A python string of characters to append to each output
            filename (optional)
        valuation_container (boolean): Indicates whether the model includes
            valuation
        land_gridPts_path (str): A CSV file path containing the Landing and
            Power Grid Connection Points table. (required for Valuation)
        machine_econ_path (str): A CSV file path for the machine economic
            parameters table. (required for Valuation)
        number_of_machines (int): An integer specifying the number of machines
            for a wave farm site. (required for Valuation)

    Example Args Dictionary::

        {
            'workspace_dir': 'path/to/workspace_dir',
            'wave_base_data_path': 'path/to/base_data_dir',
            'analysis_area_path': 'West Coast of North America and Hawaii',
            'aoi_path': 'path/to/shapefile',
            'machine_perf_path': 'path/to/csv',
            'machine_param_path': 'path/to/csv',
            'dem_path': 'path/to/raster',
            'suffix': '_results',
            'valuation_container': True,
            'land_gridPts_path': 'path/to/csv',
            'machine_econ_path': 'path/to/csv',
            'number_of_machines': 28,
        }

    """

    # Create the Output and Intermediate directories if they do not exist.
    workspace = args['workspace_dir']
    output_dir = os.path.join(workspace, 'output')
    intermediate_dir = os.path.join(workspace, 'intermediate')
    utils.make_directories([intermediate_dir, output_dir])

    # Append a _ to the suffix if it's not empty and doesn't already have one
    file_suffix = utils.make_suffix_string(args, 'suffix')

    # Get the path for the DEM
    dem_path = args['dem_path']

    # Create a dictionary that stores the wave periods and wave heights as
    # arrays. Also store the amount of energy the machine produces
    # in a certain wave period/height state as a 2D array
    machine_perf_dict = {}
    machine_perf_data = pandas.read_csv(args['machine_perf_path'])
    # Get the column field which is the first row in the file
    # and specifies the range of wave periods
    machine_perf_dict['periods'] = machine_perf_data.columns.values[1:]
    # Build up the row field by taking the first element in each row
    # This is the range of heights
    machine_perf_dict['heights'] = machine_perf_data.iloc[:, 0].values
    # Set the key for storing the machine's performance
    for i in range(len(machine_perf_dict['heights'])):
        bin_matrix_row = machine_perf_data.iloc[i, 1:].values
        # Expand the dimension from (N,) to (N,1)
        bin_matrix_row = numpy.expand_dims(bin_matrix_row, axis=0)
        # Concatenate each row along axis 0
        if i == 0:
            machine_perf_dict['bin_matrix'] = bin_matrix_row
        else:
            machine_perf_dict['bin_matrix'] = numpy.concatenate(
                (machine_perf_dict['bin_matrix'], bin_matrix_row))

    # Check if x and y dimensions of the bin_matrix array equal the size of
    # heights and periods
    if machine_perf_dict['bin_matrix'].shape != (
            machine_perf_dict['heights'].size,
            machine_perf_dict['periods'].size):
        raise ValueError(
            'Please make sure all values are entered properly in the Machine '
            'Performance Table.')
    LOGGER.debug('Machine Performance Rows : %s', machine_perf_dict['periods'])
    LOGGER.debug('Machine Performance Cols : %s', machine_perf_dict['heights'])

    machine_param_dict = _machine_csv_to_dict(args['machine_param_path'])

    # Check if required column fields are entered in the land grid csv file
    if 'land_gridPts_path' in args:
        # Create a grid_land_data dataframe for later use in valuation
        grid_land_data = pandas.read_csv(args['land_gridPts_path'])
        required_col_names = ['ID', 'TYPE', 'LAT', 'LONG', 'LOCATION']
        grid_land_data, missing_grid_land_fields = _get_validated_dataframe(
            args['land_gridPts_path'], required_col_names)
        if missing_grid_land_fields:
            raise ValueError(
                'The following column fields are missing from the Grid '
                'Connection Points File: %s' % missing_grid_land_fields)

    if 'valuation_container' in args:
        machine_econ_dict = _machine_csv_to_dict(args['machine_econ_path'])
        # Extract the machine economic parameters
        cap_max = float(machine_econ_dict['capmax'])  # maximum capacity
        capital_cost = float(machine_econ_dict['cc'])  # capital cost
        cml = float(machine_econ_dict['cml'])  # cost of mooring lines
        cul = float(machine_econ_dict['cul'])  # cost of underwater cable
        col = float(machine_econ_dict['col'])  # cost of overland cable
        omc = float(machine_econ_dict['omc'])  # operating & maintenance cost
        price = float(machine_econ_dict['p'])  # price of electricity
        drate = float(machine_econ_dict['r'])  # discount rate
        smlpm = float(machine_econ_dict['smlpm'])  # slack-moored

    # Build up a dictionary of possible analysis areas where the key
    # is the analysis area selected and the value is a dictionary
    # that stores the related paths to the needed inputs
    wave_base_data_path = args['wave_base_data_path']
    analysis_dict = {
        'West Coast of North America and Hawaii': {
            'point_vector':
            os.path.join(wave_base_data_path, 'NAmerica_WestCoast_4m.shp'),
            'extract_vector':
            os.path.join(wave_base_data_path, 'WCNA_extract.shp'),
            'ww3_path':
            os.path.join(wave_base_data_path, 'NAmerica_WestCoast_4m.txt.bin')
        },
        'East Coast of North America and Puerto Rico': {
            'point_vector':
            os.path.join(wave_base_data_path, 'NAmerica_EastCoast_4m.shp'),
            'extract_vector':
            os.path.join(wave_base_data_path, 'ECNA_extract.shp'),
            'ww3_path':
            os.path.join(wave_base_data_path, 'NAmerica_EastCoast_4m.txt.bin')
        },
        'North Sea 4 meter resolution': {
            'point_vector':
            os.path.join(wave_base_data_path, 'North_Sea_4m.shp'),
            'extract_vector':
            os.path.join(wave_base_data_path, 'North_Sea_4m_Extract.shp'),
            'ww3_path':
            os.path.join(wave_base_data_path, 'North_Sea_4m.bin')
        },
        'North Sea 10 meter resolution': {
            'point_vector':
            os.path.join(wave_base_data_path, 'North_Sea_10m.shp'),
            'extract_vector':
            os.path.join(wave_base_data_path, 'North_Sea_10m_Extract.shp'),
            'ww3_path':
            os.path.join(wave_base_data_path, 'North_Sea_10m.bin')
        },
        'Australia': {
            'point_vector':
            os.path.join(wave_base_data_path, 'Australia_4m.shp'),
            'extract_vector':
            os.path.join(wave_base_data_path, 'Australia_Extract.shp'),
            'ww3_path':
            os.path.join(wave_base_data_path, 'Australia_4m.bin')
        },
        'Global': {
            'point_vector':
            os.path.join(wave_base_data_path, 'Global.shp'),
            'extract_vector':
            os.path.join(wave_base_data_path, 'Global_extract.shp'),
            'ww3_path':
            os.path.join(wave_base_data_path, 'Global_WW3.txt.bin')
        }
    }

    # Get the String value for the analysis area provided from the dropdown
    # menu in the user interface
    analysis_area_path = args['analysis_area_path']
    # Use the analysis area String to get the path's to the wave seastate data,
    # the wave point shapefile, and the polygon extract shapefile
    wave_seastate_bins = _binary_wave_data_to_dict(
        analysis_dict[analysis_area_path]['ww3_path'])
    analysis_area_points_path = analysis_dict[analysis_area_path][
        'point_vector']
    analysis_area_extract_path = analysis_dict[analysis_area_path][
        'extract_vector']

    # Make a copy of the wave point shapefile so that the original input is
    # not corrupted when we clip the vector
    wave_vector_path = os.path.join(
        intermediate_dir, 'WEM_InputOutput_Pts%s.shp' % file_suffix)
    if os.path.isfile(wave_vector_path):
        os.remove(wave_vector_path)
    analysis_area_vector = gdal.OpenEx(analysis_area_points_path,
                                       gdal.OF_VECTOR)
    drv = gdal.GetDriverByName('ESRI Shapefile')
    drv.CreateCopy(wave_vector_path, analysis_area_vector)
    analysis_area_vector = None

    # Set the source projection for a coordinate transformation
    # to the input projection from the wave watch point shapefile
    analysis_area_sr = _get_vector_spatial_ref(analysis_area_points_path)

    # This if/else statement differentiates between having an AOI or doing
    # a broad run on all the wave points specified by args['analysis_area'].
    if 'aoi_path' not in args:
        LOGGER.info('AOI not provided.')

        # The path to a polygon shapefile that specifies the broader AOI
        aoi_vector_path = analysis_area_extract_path

        # Set the pixel size to that of DEM, to be used for creating rasters
        target_pixel_size = pygeoprocessing.get_raster_info(
            dem_path)['pixel_size']

        # Create a coordinate transformation, because it is used below when
        # indexing the DEM
        aoi_sr = _get_vector_spatial_ref(aoi_vector_path)
        aoi_sr_wkt = aoi_sr.ExportToWkt()
        coord_trans, coord_trans_opposite = _get_coordinate_transformation(
            analysis_area_sr, aoi_sr)
    else:
        LOGGER.info('AOI provided.')
        aoi_vector_path = args['aoi_path']
        # Create a coordinate transformation from the projection of the given
        # wave energy point shapefile, to the AOI's projection
        aoi_sr = _get_vector_spatial_ref(aoi_vector_path)
        aoi_sr_wkt = aoi_sr.ExportToWkt()

        # Clip the wave data shapefile by the bounds provided from the AOI
        _clip_vector_by_vector(
            analysis_area_points_path, aoi_vector_path,
            wave_vector_path, aoi_sr_wkt, intermediate_dir)

        # Clip the AOI to the Extract shape to make sure the output results do
        # not show extrapolated values outside the bounds of the points
        aoi_clipped_to_extract_path = os.path.join(
            intermediate_dir,
            'aoi_clipped_to_extract_path%s.shp' % file_suffix)
        _clip_vector_by_vector(
            aoi_vector_path, analysis_area_extract_path,
            aoi_clipped_to_extract_path, aoi_sr_wkt, intermediate_dir)
        # Replace the AOI path with the clipped AOI path
        aoi_vector_path = aoi_clipped_to_extract_path

        coord_trans, coord_trans_opposite = _get_coordinate_transformation(
            analysis_area_sr, aoi_sr)

        # Get the size of the pixels in meters, to be used for creating
        # projected wave power and wave energy capacity rasters
        target_pixel_size = _pixel_size_helper(
            wave_vector_path, coord_trans, coord_trans_opposite, dem_path)
        # new_px_size = _convert_degree_pixel_size_to_meters(dem_path, wave_vector_path)
        # pdb.set_trace()

    LOGGER.debug('target_pixel_size: %s, target_projection: %s',
                 target_pixel_size, aoi_sr_wkt)

    def _index_raster_value_to_point_vector(
            base_point_vector_path, base_raster_path, field_name, coord_trans):
        """Add the values of a raster to the field of vector point features.

        Values are recorded in the attribute field of the vector. If a value is
        larger than or equal to 0, the feature will be deleted.

        Parameters:
            base_point_vector_path (str): a path to an ogr point shapefile
            base_raster_path(str): a path to a GDAL dataset
            field_name (str): the name of the new field that will be
                added to the point feature
            coord_trans (osr.CoordinateTransformation): a coordinate
                transformation used to make sure that vector and raster are
                in the same unit

        Returns:
            None

        """
        base_vector = gdal.OpenEx(base_point_vector_path, 1)
        raster_gt = pygeoprocessing.get_raster_info(base_raster_path)[
            'geotransform']

        # Create a new field for the raster attribute
        field_defn = ogr.FieldDefn(field_name, ogr.OFTReal)
        field_defn.SetWidth(24)
        field_defn.SetPrecision(11)

        base_layer = base_vector.GetLayer()
        base_layer.CreateField(field_defn)
        feature = base_layer.GetNextFeature()

        for _, block_matrix in pygeoprocessing.iterblocks(base_raster_path):
            # For all the features (points) add the proper raster value
            while feature is not None:
                field_index = feature.GetFieldIndex(field_name)
                geom = feature.GetGeometryRef()
                geom_x, geom_y = geom.GetX(), geom.GetY()

                # Transform two points into meters
                point_decimal_degree = coord_trans.TransformPoint(
                    geom_x, geom_y)

                # To get proper raster value we must index into the dem matrix
                # by getting where the point is located in terms of the matrix
                i = int(
                    (point_decimal_degree[0] - raster_gt[0]) / raster_gt[1])
                j = int(
                    (point_decimal_degree[1] - raster_gt[3]) / raster_gt[5])
                raster_value = block_matrix[j][i]
                # There are cases where the DEM may be too coarse and thus a
                # wave energy point falls on land. If the raster value taken is
                # greater than or equal to zero we need to delete that point as
                # it should not be used in calculations
                if raster_value >= 0.0:
                    base_layer.DeleteFeature(feature.GetFID())
                else:
                    feature.SetField(int(field_index), float(raster_value))
                    base_layer.SetFeature(feature)
                feature = base_layer.GetNextFeature()

        # It is not enough to just delete a feature from the layer. The
        # database where the information is stored must be re-packed so that
        # feature entry is properly removed
        base_vector.ExecuteSQL('REPACK ' + base_layer.GetName())
        base_layer = None
        base_vector = None

    # We do all wave power calculations by manipulating the fields in
    # the wave data shapefile, thus we need to add proper depth values
    # from the raster DEM
    LOGGER.info('Adding DEPTH_M field to the wave shapefile from the DEM')
    # Add the depth value to the wave points by indexing into the DEM dataset
    _index_raster_value_to_point_vector(
        wave_vector_path, dem_path, 'DEPTH_M', coord_trans_opposite)

    # Generate an interpolate object for wave_energy_capacity
    LOGGER.info('Interpolating machine performance table.')
    energy_interp = _wave_energy_interp(wave_seastate_bins, machine_perf_dict)

    # Create a dictionary with the wave energy capacity sums from each location
    LOGGER.info('Calculating Captured Wave Energy.')
    energy_cap = _wave_energy_capacity_to_dict(
        wave_seastate_bins, energy_interp, machine_param_dict)

    # Add the sum as a field to the shapefile for the corresponding points
    LOGGER.info('Adding the wave energy sums to the WaveData shapefile')
    _captured_wave_energy_to_vector(energy_cap, wave_vector_path)

    # Calculate wave power for each wave point and add it as a field
    # to the shapefile
    LOGGER.info('Calculating Wave Power.')
    _compute_wave_power(wave_vector_path)

    # Intermediate/final output paths for wave energy and wave power rasters
    unclipped_energy_raster_path = os.path.join(
        intermediate_dir, 'unclipped_capwe_mwh%s.tif' % file_suffix)
    unclipped_power_raster_path = os.path.join(
        intermediate_dir, 'unclipped_wp_kw%s.tif' % file_suffix)
    energy_raster_path = os.path.join(
        output_dir, 'capwe_mwh%s.tif' % file_suffix)
    wave_power_raster_path = os.path.join(
        output_dir, 'wp_kw%s.tif' % file_suffix)

    # Create blank rasters bounded by the shape file of analysis area
    pygeoprocessing.create_raster_from_vector_extents(
        aoi_vector_path, unclipped_energy_raster_path, target_pixel_size,
        _TARGET_PIXEL_TYPE, _NODATA)

    pygeoprocessing.create_raster_from_vector_extents(
        aoi_vector_path, unclipped_power_raster_path, target_pixel_size,
        _TARGET_PIXEL_TYPE, _NODATA)

    # Interpolate wave energy and power from the shapefile over the rasters
    LOGGER.info('Interpolate wave power and wave energy capacity onto rasters')

    pygeoprocessing.interpolate_points(
        wave_vector_path, 'CAPWE_MWHY',
        (unclipped_energy_raster_path, 1), 'near')

    pygeoprocessing.interpolate_points(
        wave_vector_path, 'WE_kWM', (unclipped_power_raster_path, 1),
        'near')

    # Clip wave energy and power rasters to the aoi vector
    target_resample_method = 'near'
    pygeoprocessing.warp_raster(
        unclipped_energy_raster_path,
        target_pixel_size,
        energy_raster_path,
        target_resample_method,
        vector_mask_options={'mask_vector_path': aoi_vector_path}
        )
    pygeoprocessing.warp_raster(
        unclipped_power_raster_path,
        target_pixel_size,
        wave_power_raster_path,
        target_resample_method,
        vector_mask_options={'mask_vector_path': aoi_vector_path}
        )

    # Create the percentile rasters for wave energy and wave power
    # These values are hard coded in because it's specified explicitly in
    # the user's guide what the percentile ranges are and what the units
    # will be.
    percentiles = [25, 50, 75, 90]
    capwe_units_short = ' MWh/yr'
    capwe_units_long = 'megawatt hours per year'
    wp_units_short = ' kW/m'
    wp_units_long = 'wave power per unit width of wave crest length'
    starting_percentile_range = '1'

    # Paths for wave energy and wave power percentile rasters
    wp_rc_path = os.path.join(output_dir, 'wp_rc%s.tif' % file_suffix)
    capwe_rc_path = os.path.join(output_dir, 'capwe_rc%s.tif' % file_suffix)

    _create_percentile_rasters(
        energy_raster_path, capwe_rc_path, capwe_units_short,
        capwe_units_long, starting_percentile_range, percentiles)

    _create_percentile_rasters(
        wave_power_raster_path, wp_rc_path, wp_units_short, wp_units_long,
        starting_percentile_range, percentiles)

    LOGGER.info('Completed Wave Energy Biophysical')

    if 'valuation_container' not in args:
        LOGGER.info('Valuation not selected')
        # The rest of the function is valuation, so we can quit now
        return
    else:
        LOGGER.info('Valuation selected')

    # Output path for landing point shapefile
    land_vector_path = os.path.join(
        output_dir, 'LandPts_prj%s.shp' % file_suffix)
    # Output path for grid point shapefile
    grid_vector_path = os.path.join(
        output_dir, 'GridPts_prj%s.shp' % file_suffix)
    # Output path for the projected net present value raster
    npv_proj_path = os.path.join(intermediate_dir,
                                 'npv_not_clipped%s.tif' % file_suffix)
    # Path for the net present value percentile raster
    npv_rc_path = os.path.join(output_dir, 'npv_rc%s.tif' % file_suffix)

    # Number of machines for a given wave farm
    units = int(args['number_of_machines'])

    grid_data = grid_land_data.loc[grid_land_data['TYPE'].str.lower() ==
                                   'grid']
    land_data = grid_land_data.loc[grid_land_data['TYPE'].str.lower() ==
                                   'land']

    grid_dict = grid_data.to_dict('index')
    land_dict = land_data.to_dict('index')

    # Make a point shapefile for grid points
    LOGGER.info('Creating Grid Points Shapefile.')
    _dict_to_point_vector(
        grid_dict, grid_vector_path, 'grid_points', aoi_sr, coord_trans)

    # Make a point shapefile for landing points.
    LOGGER.info('Creating Landing Points Shapefile.')
    _dict_to_point_vector(
        land_dict, land_vector_path, 'land_points', aoi_sr, coord_trans)

    # Get the coordinates of points of wave, land, and grid vectors
    wave_points = _get_points_geometries(wave_vector_path)
    land_points = _get_points_geometries(land_vector_path)
    grid_points = _get_points_geometries(grid_vector_path)

    # Calculate the minimum distances between the relative point groups
    LOGGER.info('Calculating Min Distances.')
    wave_to_land_dist, wave_to_land_id = _calculate_min_distances(
        wave_points, land_points)
    land_to_grid_dist, _ = _calculate_min_distances(land_points, grid_points)

    def _add_distance_fields_to_vector(
            wave_base_vector_path, ocean_to_land_dist, land_to_grid_dist):
        """Add two fields to the wave point shapefile.

        The two fields are the distance from ocean to land, and the distance
        from land to grid.

        Parameters:
            wave_base_vector_path (str): a path to the wave points shapefile
            ocean_to_land_dist (np.array): an array of distance values
            land_to_grid_dist (np.array): an array of distance values

        Returns:
            None

        """
        wave_vector = gdal.OpenEx(wave_base_vector_path, 1)
        wave_layer = wave_vector.GetLayer(0)
        # Add three new fields to the shapefile that will store the distances
        for field in ['W2L_MDIST', 'LAND_ID', 'L2G_MDIST']:
            field_defn = ogr.FieldDefn(field, ogr.OFTReal)
            field_defn.SetWidth(24)
            field_defn.SetPrecision(11)
            wave_layer.CreateField(field_defn)

        # For each feature in the shapefile add the corresponding distance
        # from wave_to_land_dist and land_to_grid_dist calculated above
        iterate_feat = 0
        wave_layer.ResetReading()
        feature = wave_layer.GetNextFeature()
        while feature is not None:
            wave_to_land_index = feature.GetFieldIndex('W2L_MDIST')
            land_to_grid_index = feature.GetFieldIndex('L2G_MDIST')
            id_index = feature.GetFieldIndex('LAND_ID')

            land_id = int(wave_to_land_id[iterate_feat])

            feature.SetField(wave_to_land_index,
                             ocean_to_land_dist[iterate_feat])
            feature.SetField(land_to_grid_index, land_to_grid_dist[land_id])
            feature.SetField(id_index, land_id)

            iterate_feat = iterate_feat + 1

            wave_layer.SetFeature(feature)
            feature = None
            feature = wave_layer.GetNextFeature()

        wave_layer = None
        wave_vector = None

    _add_distance_fields_to_vector(wave_vector_path, wave_to_land_dist,
                                   land_to_grid_dist)

    def _calc_npv_wave(annual_revenue, annual_cost):
        """Calculate NPV for a wave farm based on the annual revenue and cost.

        Parameters:
            annual_revenue (numpy.array): an array of the annual revenue for
                the first 25 years
            annual_cost (numpy.array): an array of the annual cost for the
                first 25 years

        Returns: The Total NPV which is the sum of all 25 years

        """
        # The discount rate calculation for the npv equations
        rho = 1.0 / (1.0 + drate)

        npv = []
        for t in range(_LIFE_SPAN):
            npv.append(rho**t * (annual_revenue[i] - annual_cost[i]))
        return sum(npv)

    def _add_npv_field_to_vector(wave_points_path):
        """Compute and add NPV to the vector.

        Also compute the total captured wave energy for the entire farm.

        Parameters:
            wave_points_path (str): a path to the wave energy points with
                fields for calculating NPV.

        Returns:
            None

        """
        wave_point_vector = gdal.OpenEx(wave_points_path, 1)
        wave_point_layer = wave_point_vector.GetLayer()
        # Add Net Present Value field, Total Captured Wave Energy field, and
        # Units field to shapefile
        for field_name in ['NPV_25Y', 'CAPWE_ALL', 'UNITS']:
            field_defn = ogr.FieldDefn(field_name, ogr.OFTReal)
            field_defn.SetWidth(24)
            field_defn.SetPrecision(11)
            wave_point_layer.CreateField(field_defn)
        wave_point_layer.ResetReading()
        feat_npv = wave_point_layer.GetNextFeature()

        # For all the wave farm sites, calculate NPV and write to shapefile
        LOGGER.info('Calculating the Net Present Value.')
        while feat_npv is not None:
            depth_index = feat_npv.GetFieldIndex('DEPTH_M')
            wave_to_land_index = feat_npv.GetFieldIndex('W2L_MDIST')
            land_to_grid_index = feat_npv.GetFieldIndex('L2G_MDIST')
            captured_wave_energy_index = feat_npv.GetFieldIndex('CAPWE_MWHY')
            npv_index = feat_npv.GetFieldIndex('NPV_25Y')
            capwe_all_index = feat_npv.GetFieldIndex('CAPWE_ALL')
            units_index = feat_npv.GetFieldIndex('UNITS')

            depth = feat_npv.GetFieldAsDouble(depth_index)
            wave_to_land = feat_npv.GetFieldAsDouble(wave_to_land_index)
            land_to_grid = feat_npv.GetFieldAsDouble(land_to_grid_index)
            captured_wave_energy = feat_npv.GetFieldAsDouble(
                captured_wave_energy_index)
            capwe_all_result = captured_wave_energy * units

            # Create a numpy array of length 25, filled with the captured wave
            # energy in kW/h. Represents the lifetime of this wave farm.
            captured_we = numpy.ones(
                _LIFE_SPAN) * (int(captured_wave_energy) * 1000.0)
            # It is expected that there is no revenue from the first year
            captured_we[0] = 0

            # Compute values to determine NPV
            lenml = 3.0 * numpy.absolute(depth)
            install_cost = units * cap_max * capital_cost
            mooring_cost = smlpm * lenml * cml * units
            trans_cost = (wave_to_land * cul / 1000.0) + (
                land_to_grid * col / 1000.0)
            initial_cost = install_cost + mooring_cost + trans_cost
            annual_revenue = price * units * captured_we
            annual_cost = omc * captured_we * units

            # The first year's costs are the initial start up costs
            annual_cost[0] = initial_cost

            npv_result = _calc_npv_wave(annual_revenue, annual_cost) / 1000.0
            pdb.set_trace()
            feat_npv.SetField(npv_index, npv_result)
            feat_npv.SetField(capwe_all_index, capwe_all_result)
            feat_npv.SetField(units_index, units)

            wave_point_layer.SetFeature(feat_npv)
            feat_npv = None
            feat_npv = wave_point_layer.GetNextFeature()

        wave_point_layer = None
        wave_point_vector = None

    _add_npv_field_to_vector(wave_vector_path)

    # Create a blank raster from the extents of the wave farm shapefile
    LOGGER.info('Creating Raster From Vector Extents')
    pygeoprocessing.create_raster_from_vector_extents(
        wave_vector_path, npv_proj_path, target_pixel_size, _TARGET_PIXEL_TYPE,
        _NODATA)
    LOGGER.info('Completed Creating Raster From Vector Extents')

    # Interpolate the NPV values based on the dimensions and corresponding
    # points of the raster, then write the interpolated values to the raster
    LOGGER.info('Generating Net Present Value Raster.')
    pygeoprocessing.interpolate_points(wave_vector_path, 'NPV_25Y',
                                       (npv_proj_path, 1), 'near')

    npv_out_path = os.path.join(output_dir, 'npv_usd%s.tif' % file_suffix)

    # Clip the raster to the convex hull polygon
    pygeoprocessing.warp_raster(
        npv_proj_path,
        target_pixel_size,
        npv_out_path,
        target_resample_method,
        vector_mask_options={'mask_vector_path': aoi_vector_path}
        )

    # Create the percentile raster for net present value
    percentiles = [25, 50, 75, 90]

    _create_percentile_rasters(npv_out_path, npv_rc_path, ' (US$)',
                               ' thousands of US dollars (US$)', '1',
                               percentiles)

    LOGGER.info('End of Wave Energy Valuation.')


def _get_validated_dataframe(csv_path, field_list):
    """Return a dataframe with upper cased fields, and a list of missing fields.

    Parameters:
        csv_path (str): path to the csv to be converted to a dataframe.
        field_list (list): a list of fields in string format.

    Returns:
        dataframe (pandas.DataFrame): from csv with upper-cased fields.
        missing_fields (list): missing fields as string format in dataframe.

    """
    dataframe = pandas.read_csv(csv_path)
    field_list = [field.upper() for field in field_list]
    dataframe.columns = [
        col_name.upper() for col_name in dataframe.columns]
    missing_fields = []
    for field in field_list:
        if field not in dataframe.columns:
            missing_fields.append(field)
    return dataframe, missing_fields


def _dict_to_point_vector(base_dict_data, target_vector_path, layer_name,
                          target_sr, coord_trans):
    """Given a dictionary of data create a point shapefile that represents it.

    Parameters:
        base_dict_data (dict): a  dictionary with the wind data, where the keys
            are tuples of the lat/long coordinates:
            {
            1 : {'TYPE':'GRID', 'LAT':49, 'LONG':-126, 'LOCATION':'Ucluelet'},
            2 : {'TYPE':'GRID', 'LAT':50, 'LONG':-127, 'LOCATION':'Tofino'},
            }
        layer_name (str): the name of the layer.
        target_vector_path (str): path to the output destination of the
            shapefile.
        target_sr (str): target spatial reference in well-known text format
        coord_trans (OGRCoordinateTransformation): a coordinate transformation
            from source to target spatial reference

    Returns:
        None

    """
    # If the target_vector_path exists delete it
    if os.path.isfile(target_vector_path):
        driver = ogr.GetDriverByName('ESRI Shapefile')
        driver.DeleteDataSource(target_vector_path)

    LOGGER.info('Creating new vector')
    output_driver = ogr.GetDriverByName('ESRI Shapefile')
    output_vector = output_driver.CreateDataSource(target_vector_path)
    target_layer = output_vector.CreateLayer(layer_name, target_sr,
                                             ogr.wkbPoint)

    # Construct a dictionary of field names and their corresponding types
    field_dict = {
        'ID': ogr.OFTInteger,
        'TYPE': ogr.OFTString,
        'LAT': ogr.OFTReal,
        'LONG': ogr.OFTReal,
        'LOCATION': ogr.OFTString
    }

    LOGGER.info('Creating fields for the vector')
    for field_name in ['ID', 'TYPE', 'LAT', 'LONG', 'LOCATION']:
        target_field = ogr.FieldDefn(field_name, field_dict[field_name])
        target_layer.CreateField(target_field)

    LOGGER.info('Entering iteration to create and set the features')
    # For each inner dictionary (for each point) create a point
    for point_dict in base_dict_data.itervalues():
        latitude = float(point_dict['LAT'])
        longitude = float(point_dict['LONG'])
        # When projecting to WGS84, extents -180 to 180 are used for longitude.
        # In case input longitude is from -360 to 0 convert
        if longitude < -180:
            longitude += 360
        geom = ogr.Geometry(ogr.wkbPoint)
        geom.AddPoint_2D(longitude, latitude)
        geom.Transform(coord_trans)

        output_feature = ogr.Feature(target_layer.GetLayerDefn())
        target_layer.CreateFeature(output_feature)

        for field_name in point_dict:
            field_index = output_feature.GetFieldIndex(field_name)
            output_feature.SetField(field_index, point_dict[field_name])
        output_feature.SetGeometryDirectly(geom)
        target_layer.SetFeature(output_feature)
        output_feature = None

    output_vector = None
    LOGGER.info('Finished _dict_to_point_vector')


def _get_points_geometries(base_vector_path):
    """Retrieve the XY coordinates from a point shapefile.

    The X and Y values from each point feature in the vector are stored in pair
    as [x_location,y_location] in a numpy array.

    Parameters:
        base_vector_path (str): a path to an OGR shapefile

    Returns:
        an array of points, representing the geometry of each point in the
            shapefile.

    """
    points = []
    base_vector = gdal.OpenEx(base_vector_path)
    base_layer = base_vector.GetLayer(0)
    feat = base_layer.GetNextFeature()
    while feat is not None:
        x_location = float(feat.GetGeometryRef().GetX())
        y_location = float(feat.GetGeometryRef().GetY())
        points.append([x_location, y_location])
        feat = None
        feat = base_layer.GetNextFeature()
    base_layer = None
    base_vector = None

    return numpy.array(points)


def _calculate_min_distances(xy_1, xy_2):
    """For all points in xy_1, this function calculates the distance from point
    xy_1 to various points in xy_2, and stores the shortest distances found in
    a list min_dist. The function also stores the index from which ever point
    in xy_2 was closest, as an id in a list that corresponds to min_dist.

    Parameters:
        xy_1 (numpy.array): An array of points in the form [x,y]
        xy_2 (numpy.array): An array of points in the form [x,y]

    Returns:
        A numpy array of shortest distances and a numpy array of indexes
        corresponding to the array of shortest distances

    """
    # Create two numpy array of zeros with length set to as many points in xy_1
    min_dist = numpy.zeros(len(xy_1))
    min_id = numpy.zeros(len(xy_1))

    # For all points xy_point in xy_1 calculate the distance from xy_point to
    # xy_2 and save the shortest distance found.
    for index, xy_point in enumerate(xy_1):
        dists = numpy.sqrt(numpy.sum((xy_point - xy_2)**2, axis=1))
        min_dist[index], min_id[index] = dists.min(), dists.argmin()
    return min_dist, min_id


def _binary_wave_data_to_dict(wave_file_path):
    """Convert a pickled binary WW3 text file into a dictionary.

    The dictionary's keys are the corresponding (I,J) values and the value is
    a two-dimensional array representing a matrix of the number of hours a
    seastate occurs over a 5 year period. The row and column fields are
    extracted once and stored in the dictionary as well.

    Parameters:
        wave_file_path (str): path to a pickled binary WW3 file.

    Returns:
        wave_dict (dict): a dictionary of matrices representing hours of
            specific seastates, as well as the period and height ranges.
            It has the following structure:
               {'periods': [1,2,3,4,...],
                'heights': [.5,1.0,1.5,...],
                'bin_matrix': { (i0,j0): [[2,5,3,2,...], [6,3,4,1,...],...],
                                (i1,j1): [[2,5,3,2,...], [6,3,4,1,...],...],
                                 ...
                                (in, jn): [[2,5,3,2,...], [6,3,4,1,...],...]
                              }
               }

    """
    LOGGER.info('Extrapolating wave data from text to a dictionary')
    wave_file = open(wave_file_path, 'rb')
    wave_dict = {}
    # Create a key that hosts another dictionary where the matrix
    # representation of the seastate bins will be saved
    wave_dict['bin_matrix'] = {}
    wave_array = None
    wave_periods = []
    wave_heights = []
    key = None

    # get rows,cols
    row_col_bin = wave_file.read(8)
    n_cols, n_rows = struct.unpack('ii', row_col_bin)

    # get the periods and heights
    line = wave_file.read(n_cols * 4)

    wave_periods = list(struct.unpack('f' * n_cols, line))
    line = wave_file.read(n_rows * 4)
    wave_heights = list(struct.unpack('f' * n_rows, line))

    key = None
    while True:
        line = wave_file.read(8)
        if not line:
            # end of file
            wave_dict['bin_matrix'][key] = numpy.array(wave_array)
            break

        if key is not None:
            wave_dict['bin_matrix'][key] = numpy.array(wave_array)

        # Clear out array
        wave_array = []

        key = struct.unpack('ii', line)

        for _ in itertools.repeat(None, n_rows):
            line = wave_file.read(n_cols * 4)
            array = list(struct.unpack('f' * n_cols, line))
            wave_array.append(array)

    wave_file.close()
    # Add row/col field to dictionary
    LOGGER.debug('WaveData col %s', wave_periods)
    wave_dict['periods'] = numpy.array(wave_periods, dtype='f')
    LOGGER.debug('WaveData row %s', wave_heights)
    wave_dict['heights'] = numpy.array(wave_heights, dtype='f')
    LOGGER.info('Finished extrapolating wave data to dictionary.')
    return wave_dict


def _machine_csv_to_dict(machine_csv_path):
    """Create a dictionary from the table in machine csv file.

    The dictionary's keys are the 'NAME' from the machine table and its values
    are from the corresponding 'VALUE' field.

    Parameters:
        machine_csv_path (str): path to the input machine CSV file.

    Returns:
        None.

    """
    machine_dict = {}
    machine_data = pandas.read_csv(machine_csv_path, index_col=0)
    machine_data.columns = machine_data.columns.str.lower()
    if 'value' not in machine_data.columns:
        raise ValueError('Please make sure that the "VALUE" column is in the '
                         'Machine Parameters Table.')
    # remove underscore from the keys and make them lowercased
    machine_data.index = machine_data.index.str.strip()
    machine_data.index = machine_data.index.str.lower()
    machine_dict = machine_data.to_dict('index')
    for key in machine_dict.keys():
        machine_dict[key] = machine_dict[key]['value']
    return machine_dict


def _get_vector_spatial_ref(base_vector_path):
    """Get the spatial reference of an OGR vector (datasource).

    Parameters:
        base_vector_path (str): a path to an ogr vector

    Returns:
        spat_ref: a spatial reference

    """
    vector = gdal.OpenEx(base_vector_path, gdal.OF_VECTOR)
    layer = vector.GetLayer(0)
    spat_ref = layer.GetSpatialRef()
    layer = None
    vector = None
    return spat_ref


def _get_coordinate_transformation(source_sr, target_sr):
    """Create coordinate transformations between two spatial references.

    One transformation is from source to target, and the other from target to
    source.

    Parameters:
        source_sr (osr.SpatialReference): A spatial reference
        target_sr (osr.SpatialReference): A spatial reference

    Returns:
        A tuple: coord_trans (source to target) and coord_trans_opposite
        (target to source)

    """
    coord_trans = osr.CoordinateTransformation(source_sr, target_sr)
    coord_trans_opposite = osr.CoordinateTransformation(target_sr, source_sr)
    return (coord_trans, coord_trans_opposite)


def _create_percentile_rasters(base_raster_path, target_raster_path,
                               units_short, units_long, start_value,
                               percentile_list):
    """Creates a percentile (quartile) raster based on the raster_dataset.

    An attribute table is also constructed for the raster_dataset that displays
    the ranges provided by taking the quartile of values.

    Parameters:
        base_raster_path (str): path to a GDAL raster with data of type
            integer
        target_raster_path (str): path to the destination of the new raster.
        units_short (str): The shorthand for the units of the raster values,
            ex: kW/m.
        units_long (str): The description of the units of the raster values,
            ex: wave power per unit width of wave crest length (kW/m).
        start_value (str): The first value that goes to the first percentile
            range (start_value: percentile_one)
        percentile_list (list): A list of the percentiles ranges,
            ex: [25, 50, 75, 90].

    Returns:
        None

    """
    LOGGER.info('Creating Percentile Rasters')

    # If the target_raster_path is already a file, delete it
    if os.path.isfile(target_raster_path):
        os.remove(target_raster_path)

    # Set nodata to a very small negative number
    target_nodata = -99999
    base_nodata = pygeoprocessing.get_raster_info(base_raster_path)['nodata'][
        0]

    # Get the percentile values for each percentile
    percentile_values = _calculate_percentiles_from_raster(
        base_raster_path, percentile_list)

    # Get the percentile ranges as strings so that they can be added to an
    # output table
    value_ranges = _create_value_ranges(percentile_values, start_value)

    def raster_percentile(band):
        """Take the pixels of band and group them together based on percentiles.
        """
        return numpy.where(
            band != base_nodata,
            numpy.searchsorted(percentile_values, band) + 1,  # starts from 1
            target_nodata)

    # Classify the pixels of raster_dataset into groups and write to output
    pygeoprocessing.raster_calculator([(base_raster_path, 1)],
                                      raster_percentile, target_raster_path,
                                      gdal.GDT_Int32, target_nodata)

    # Create percentile groups of how percentile ranges are classified
    # using numpy.searchsorted
    percentile_groups = numpy.arange(1, len(percentile_values) + 2)

    # Get the pixel count for each group
    pixel_count = _count_pixels_groups(target_raster_path, percentile_groups)

    LOGGER.debug('Pixel_count: %s; Percentile_groups: %s' %
                 (pixel_count, percentile_groups))

    # Initialize a dictionary where percentile groups map to a string
    # of corresponding percentile ranges. Used to create RAT
    percentile_dict = {}
    for index in xrange(len(percentile_groups)):
        percentile_dict[percentile_groups[index]] = value_ranges[index]
    value_range_field = 'Value Range (' + units_long + ',' + units_short + ')'
    _create_raster_attr_table(
        target_raster_path, percentile_dict, column_name=value_range_field)

    # Create a list of corresponding percentile ranges from the percentile list
    percentile_ranges = _create_percentile_ranges(percentile_list)

    # Initialize a dictionary to map percentile groups to percentile range
    # string and pixel count. Used for creating CSV table
    column_names = [
        'Percentile Group', 'Percentile Range', value_range_field,
        'Pixel Count'
    ]
    table_dict = dict((col_name, []) for col_name in column_names)
    for index in xrange(len(percentile_groups)):
        table_dict['Percentile Group'].append(percentile_groups[index])
        table_dict['Percentile Range'].append(percentile_ranges[index])
        table_dict[value_range_field].append(value_ranges[index])
        table_dict['Pixel Count'].append(pixel_count[index])

    table_df = pandas.DataFrame(table_dict)

    # Write dataframe to csv, with columns in designated sequence
    attribute_table_path = target_raster_path[:-4] + '.csv'
    table_df.to_csv(attribute_table_path, index=False, columns=column_names)


def _create_value_ranges(percentiles, start_value):
    """Construct the value ranges as string, which is then stored in a list.

    The list has the first range starting at 1 and the last range being greater
    than the last percentile mark.

    Parameters:
        percentiles (list): a list of the percentile marks in ascending order.
        start_value (str): the first value that goes to the first percentile
            range (start_value: percentile_one).

    Returns:
        range_values (list): a list of strings representing the ranges of the
            percentile values.

    """
    length = len(percentiles)
    range_values = []
    # Add the first range with the starting value and long description of units
    # This function will fail and cause an error if percentile list is empty
    range_first = start_value + ' - ' + str(percentiles[0])
    range_values.append(range_first)
    for index in range(length - 1):
        range_values.append(
            str(percentiles[index]) + ' - ' + str(percentiles[index + 1]))
    # Add the last range to the range of values list
    range_last = 'Greater than ' + str(percentiles[length - 1])
    range_values.append(range_last)
    LOGGER.debug('Range_values : %s', range_values)
    return range_values


def _create_percentile_ranges(percentile_list):
    """Construct the percentile ranges as Strings.

    Each string range is stored in a list that gets returned.

    Parameters:
        percentile_list (list): A list of percentiles in ascending order

    Returns:
        A list of Strings representing the ranges of the percentile values

    """
    length = len(percentile_list)
    percentile_ranges = []
    first_range = '<' + str(percentile_list[0]) + '%'
    percentile_ranges.append(first_range)
    for index in range(length - 1):
        percentile_ranges.append(
            str(percentile_list[index]) + '-' +
            str(percentile_list[index + 1]) + '%')

    # Add the last range to the percentile ranges list
    last_range = '>' + str(percentile_list[length - 1]) + '%'
    percentile_ranges.append(last_range)
    return percentile_ranges


def _compute_wave_power(base_vector_path):
    """Calculate and write the wave power based on the fields in the shapefile.

    Parameters:
        base_vector_path (str): path to a shapefile that has all the attributes
            represented in fields to calculate wave power at a specific wave
            farm.

    Returns:
        None

    """
    vector = gdal.OpenEx(base_vector_path, 1)

    # Sea water density constant (kg/m^3)
    swd = 1028
    # Gravitational acceleration (m/s^2)
    grav = 9.8
    # Constant determining the shape of a wave spectrum (see users guide pg 23)
    alfa = 0.86
    # Add a waver power field to the shapefile.
    layer = vector.GetLayer()
    field_defn = ogr.FieldDefn('WE_kWM', ogr.OFTReal)
    field_defn.SetWidth(24)
    field_defn.SetPrecision(11)
    layer.CreateField(field_defn)
    layer.ResetReading()
    feat = layer.GetNextFeature()
    # For every feature (point) calculate the wave power and add the value
    # to itself in a new field
    while feat is not None:
        height_index = feat.GetFieldIndex('HSAVG_M')
        period_index = feat.GetFieldIndex('TPAVG_S')
        depth_index = feat.GetFieldIndex('DEPTH_M')
        wp_index = feat.GetFieldIndex('WE_kWM')
        height = feat.GetFieldAsDouble(height_index)
        period = feat.GetFieldAsDouble(period_index)
        depth = feat.GetFieldAsInteger(depth_index)

        depth = numpy.absolute(depth)
        # wave frequency calculation (used to calculate wave number k)
        tem = (2.0 * math.pi) / (period * alfa)
        # wave number calculation (expressed as a function of
        # wave frequency and water depth)
        k = numpy.square(tem) / (grav * numpy.sqrt(
            numpy.tanh((numpy.square(tem)) * (depth / grav))))
        # Setting numpy overlow error to ignore because when numpy.sinh
        # gets a really large number it pushes a warning, but Rich
        # and Doug have agreed it's nothing we need to worry about.
        numpy.seterr(over='ignore')

        # wave group velocity calculation (expressed as a
        # function of wave energy period and water depth)
        wave_group_velocity = (((1 + (
            (2 * k * depth) / numpy.sinh(2 * k * depth))) * numpy.sqrt(
                (grav / k) * numpy.tanh(k * depth))) / 2)

        # Reset the overflow error to print future warnings
        numpy.seterr(over='print')

        # wave power calculation
        wave_pow = ((((swd * grav) / 16) *
                     (numpy.square(height)) * wave_group_velocity) / 1000)

        feat.SetField(wp_index, wave_pow)
        layer.SetFeature(feat)
        feat = layer.GetNextFeature()

    feat = None
    layer = None
    vector = None


def _clip_vector_by_vector(
        base_vector_path, clip_vector_path, target_clipped_vector_path,
        target_sr_wkt, work_dir):
    """Clip Shapefile Layer by second Shapefile Layer.

    Clip a shapefile layer where the output Layer inherits the projection and
    fields from the original Shapefile.

    Parameters:
        base_vector_path (str): a path to a Shapefile on disk. This is
            the Layer to clip. Must have same spatial reference as
            'clip_vector_path'.
        clip_vector_path (str): a path to a Shapefile on disk. This is
            the Layer to clip to. Must have same spatial reference as
            'base_vector_path'
        target_clipped_vector_path (str): a path on disk to write the clipped
            shapefile to. Should end with a '.shp' extension.
        target_sr_wkt (str): projection for the target_clipped_vector.
        work_dir (str): path to directory for saving temporary output files.

    Returns:
        None

    """
    if os.path.isfile(target_clipped_vector_path):
        driver = ogr.GetDriverByName('ESRI Shapefile')
        driver.DeleteDataSource(target_clipped_vector_path)

    # Create a temporary folder within work_dir for saving reprojected files
    temp_work_dir = tempfile.mkdtemp(dir=work_dir, prefix='reproject-')

    def reproject_vector(base_vector_path, target_sr_wkt, temp_work_dir):
        """Reproject the vector to target projection."""
        base_sr_wkt = pygeoprocessing.get_vector_info(base_vector_path)[
            'projection']

        if base_sr_wkt != target_sr_wkt:
            LOGGER.info('Base and target projections are different. '
                        'Reprojecting %s to %s.', base_vector_path,
                        target_sr_wkt)

            # Create path for the reprojected shapefile
            reproject_base_vector_path = os.path.join(
                temp_work_dir,
                os.path.basename(base_vector_path).replace(
                    '.shp', '_projected.shp'))
            pygeoprocessing.reproject_vector(base_vector_path, target_sr_wkt,
                                             reproject_base_vector_path)
            # Replace the base shapefile path with the reprojected path
            base_vector_path = reproject_base_vector_path

        return base_vector_path

    reproject_base_vector_path = reproject_vector(
        base_vector_path, target_sr_wkt, temp_work_dir)
    reproject_clip_vector_path = reproject_vector(
        clip_vector_path, target_sr_wkt, temp_work_dir)

    base_vector = gdal.OpenEx(reproject_base_vector_path)
    base_layer = base_vector.GetLayer()
    base_layer_defn = base_layer.GetLayerDefn()

    clip_vector = gdal.OpenEx(reproject_clip_vector_path)
    clip_layer = clip_vector.GetLayer()

    driver = ogr.GetDriverByName('ESRI Shapefile')
    target_vector = driver.CreateDataSource(target_clipped_vector_path)
    target_layer = target_vector.CreateLayer(base_layer_defn.GetName(),
                                             base_layer.GetSpatialRef())
    base_layer.Clip(clip_layer, target_layer)

    # Add in a check to make sure the intersection didn't come back empty
    if target_layer.GetFeatureCount() == 0:
        raise IntersectionError(
            "Intersection ERROR: found no intersection between base vector %s "
            "and clip vector %s." % (base_vector_path, clip_vector_path))

    base_layer = None
    clip_layer = None
    target_layer = None
    base_vector = None
    clip_vector = None
    target_vector = None

    shutil.rmtree(temp_work_dir, ignore_errors=True)


def _wave_energy_interp(wave_data, machine_perf):
    """Generate an interpolation matrix representing the machine perf table.

    The matrix is generated using new ranges from wave watch data.

    Parameters:
        wave_data (dict): A dictionary holding the new x range (period) and
            y range (height) values for the interpolation. The dictionary has
            the following structure:
                {'periods': [1,2,3,4,...],
                 'heights': [.5,1.0,1.5,...],
                 'bin_matrix': { (i0,j0): [[2,5,3,2,...], [6,3,4,1,...],...],
                                 (i1,j1): [[2,5,3,2,...], [6,3,4,1,...],...],
                                  ...
                                 (in, jn): [[2,5,3,2,...], [6,3,4,1,...],...]
                               }
                }
        machine_perf (dict): a dictionary that holds the machine performance
            information with the following keys and structure:
                machine_perf['periods'] - [1,2,3,...]
                machine_perf['heights'] - [.5,1,1.5,...]
                machine_perf['bin_matrix'] - [[1,2,3,...],[5,6,7,...],...].

    Returns:
        The interpolated matrix

    """
    # Get ranges and matrix for machine performance table
    x_range = numpy.array(machine_perf['periods'], dtype='f')
    y_range = numpy.array(machine_perf['heights'], dtype='f')
    z_matrix = numpy.array(machine_perf['bin_matrix'], dtype='f')
    # Get new ranges to interpolate to, from wave_data table
    new_x = wave_data['periods']
    new_y = wave_data['heights']

    # Interpolate machine performance table and return the interpolated matrix
    interp_z_spl = scipy.interpolate.RectBivariateSpline(
        x_range, y_range, z_matrix.transpose(), kx=1, ky=1)
    return interp_z_spl(new_x, new_y).transpose()


def _wave_energy_capacity_to_dict(wave_data, interp_z, machine_param):
    """Compute and save the wave energy capacity for each point to a dict.

    The dictionary keys are the points (i,j) and their corresponding value
    is the wave energy capacity.

    Parameters:
        wave_data (dict): A wave watch dictionary with the following structure:
                {'periods': [1,2,3,4,...],
                 'heights': [.5,1.0,1.5,...],
                 'bin_matrix': { (i0,j0): [[2,5,3,2,...], [6,3,4,1,...],...],
                                 (i1,j1): [[2,5,3,2,...], [6,3,4,1,...],...],
                                  ...
                                 (in, jn): [[2,5,3,2,...], [6,3,4,1,...],...]
                               }
                }
        interp_z (np.array): A 2D array of the interpolated values for the
            machine performance table
        machine_param (dict): A dictionary containing the restrictions for the
            machines (CapMax, TpMax, HsMax)

    Returns:
        energy_cap (dict): key - wave point, value - the wave energy capacity

    """
    energy_cap = {}

    # Get the row,col fields (ranges) for the wave watch data
    # row is wave period label
    # col is wave height label
    wave_periods = wave_data['periods']
    wave_heights = wave_data['heights']

    # Get the machine parameter restriction values
    cap_max = float(machine_param['capmax'])
    period_max = float(machine_param['tpmax'])
    height_max = float(machine_param['hsmax'])

    # It seems that the capacity max is already set to it's limit in
    # the machine performance table. However, if it needed to be
    # restricted the following line will do it
    interp_z = numpy.array(interp_z)
    interp_z[interp_z > cap_max] = cap_max

    # Set position variables to use as a check and as an end
    # point for rows/cols if restrictions limit the ranges
    period_max_index = -1
    height_max_index = -1

    # Using the restrictions find the max position (index) for period and
    # height in the wave_periods/wave_heights ranges

    for index_pos, value in enumerate(wave_periods):
        if value > period_max:
            period_max_index = index_pos
            break

    for index_pos, value in enumerate(wave_heights):
        if value > height_max:
            height_max_index = index_pos
            break

    LOGGER.debug('Position of max period : %f', period_max_index)
    LOGGER.debug('Position of max height : %f', height_max_index)

    # For all the wave watch points, multiply the occurrence matrix by the
    # interpolated machine performance matrix to get the captured wave energy
    for key, val in wave_data['bin_matrix'].iteritems():
        # Convert all values to type float
        temp_matrix = numpy.array(val, dtype='f')
        mult_matrix = numpy.multiply(temp_matrix, interp_z)
        # Set any value that is outside the restricting ranges provided by
        # machine parameters to zero
        if period_max_index != -1:
            mult_matrix[:, period_max_index:] = 0
        if height_max_index != -1:
            mult_matrix[height_max_index:, :] = 0

        # Since we are doing a cubic interpolation there is a possibility we
        # will have negative values where they should be zero. So here
        # we drive any negative values to zero.
        mult_matrix[mult_matrix < 0] = 0

        # Sum all of the values from the matrix to get the total
        # captured wave energy and convert into mega watts
        sum_we = (mult_matrix.sum() / 1000)
        energy_cap[key] = sum_we

    return energy_cap


def _captured_wave_energy_to_vector(energy_cap, wave_vector_path):
    """Add captured wave energy value from energy_cap to a field in wave_vector.

    The values are set corresponding to the same I,J values which is the key of
    the dictionary and used as the unique identifier of the shape.

    Parameters:
        energy_cap (dict): a dictionary with keys (I,J), representing the
            wave energy capacity values.
        wave_vector_path (str): path to a point geometry shapefile to
            write the new field/values to

    Returns:
        None.

    """
    cap_we_field = 'CAPWE_MWHY'
    wave_vector = gdal.OpenEx(wave_vector_path, 1)
    wave_layer = wave_vector.GetLayer()
    # Create a new field for the shapefile
    field_defn = ogr.FieldDefn(cap_we_field, ogr.OFTReal)
    field_defn.SetWidth(24)
    field_defn.SetPrecision(11)
    wave_layer.CreateField(field_defn)
    # For all of the features (points) in the shapefile, get the
    # corresponding point/value from the dictionary and set the 'capWE_Sum'
    # field as the value from the dictionary
    for feat in wave_layer:
        index_i = feat.GetFieldIndex('I')
        index_j = feat.GetFieldIndex('J')
        value_i = feat.GetField(index_i)
        value_j = feat.GetField(index_j)
        we_value = energy_cap[(value_i, value_j)]

        index = feat.GetFieldIndex(cap_we_field)
        feat.SetField(index, we_value)
        # Save the feature modifications to the layer.
        wave_layer.SetFeature(feat)
        feat = None
    wave_layer = None
    wave_vector = None


def _calculate_percentiles_from_raster(base_raster_path, percentile_list):
    """Determine the percentiles of a raster using the nearest-rank method.

    Iterate through the raster blocks and round the unique values for
    efficiency. Then add each unique value-count pair into a dictionary.
    Compute ordinal ranks given the percentile list.

    Parameters:
        base_raster_path (str): path to a gdal raster on disk
        percentile_list (list): a list of desired percentiles to lookup,
            ex: [25,50,75,90]

    Returns:
            a list of values corresponding to the percentiles from the list

    """
    nodata = pygeoprocessing.get_raster_info(base_raster_path)['nodata'][0]
    unique_value_counts = {}
    for _, block_matrix in pygeoprocessing.iterblocks(base_raster_path):
        # Sum the values with the same key in both dictionaries
        unique_values, counts = numpy.unique(block_matrix, return_counts=True)
        # Remove the nodata value from the array
        unique_values = unique_values[unique_values != nodata]
        # Round the array so the unique values won't explode the dictionary
        numpy.round(unique_values, decimals=1, out=unique_values)

        block_unique_value_counts = dict(zip(unique_values, counts))
        for value in block_unique_value_counts.keys():
            unique_value_counts[value] = unique_value_counts.get(
                value, 0) + block_unique_value_counts.get(value, 0)

    LOGGER.debug('Unique_value_counts: %s', unique_value_counts)

    # Get the total pixel count except nodata pixels
    total_count = sum(unique_value_counts.values())

    # Calculate the ordinal rank
    ordinal_rank = [
        numpy.ceil(percentile / 100.0 * total_count)
        for percentile in percentile_list
    ]

    # Get values from the ordered dictionary that correspond to the ranks
    percentile_values = []  # list for corresponding values
    ith_element = 0  # indexing the ith element in the percentile_values list
    cumulative_count = 0  # for checking if the percentile value is reached

    # Get the unique_value keys in the ascending order
    for unique_value in sorted(unique_value_counts.keys()):
        # stop iteration when all corresponding values are obtained
        if ith_element > len(ordinal_rank) - 1:
            break

        # Add count from the unique value
        count = unique_value_counts[unique_value]
        cumulative_count += count

        while ordinal_rank[ith_element] == cumulative_count or \
                ordinal_rank[ith_element] < cumulative_count:
            percentile_values.append(unique_value)
            ith_element += 1

            # stop iteration when all corresponding values are obtained
            if ith_element == len(ordinal_rank):
                break

    LOGGER.debug('Percentile_values: %s', percentile_values)
    return percentile_values


def _count_pixels_groups(raster_path, group_values):
    """Count pixels for each value in 'group_values' over a raster.

    Parameters:
        raster_path (str): path to a GDAL raster on disk
        group_values (list): unique numbers for which to get a pixel count

    Returns:
        pixel_count (list): a list of integers, where each integer at an index
            corresponds to the pixel count of the value from 'group_values'

    """
    # Initialize a list that will hold pixel counts for each group
    pixel_count = numpy.zeros(len(group_values))

    for _, block_matrix in pygeoprocessing.iterblocks(raster_path):
        # Cumulatively add the pixels count for each value in 'group_values'
        for index in xrange(len(group_values)):
            val = group_values[index]
            count_mask = numpy.zeros(block_matrix.shape)
            numpy.equal(block_matrix, val, count_mask)
            pixel_count[index] += numpy.count_nonzero(count_mask)

    return pixel_count


def _pixel_size_helper(
        base_vector_path, coord_trans, coord_trans_opposite, base_raster_path):
    """Retrieve pixel size of a raster given a vector w/ certain projection.

    Parameters:
        base_vector_path (str): path to a shapefile indicating where in the
            world we are interested in
        coord_trans (osr.CoordinateTransformation): a coordinate transformation
        coord_trans_opposite (osr.CoordinateTransformation): a coordinate
            transformation in the opposite direction of 'coord_trans'
        base_raster_path (str): path to a raster to get the pixel size from

    Returns:
        pixel_size_tuple (tuple): x and y pixel sizes of the raster given in
            the units of what base vector is projected in

    """
    vector = gdal.OpenEx(base_vector_path)
    layer = vector.GetLayer(0)

    # Get a point in the clipped vector to determine output grid size
    feat = layer.GetNextFeature()
    geom = feat.GetGeometryRef()
    reference_point_x = geom.GetX()
    reference_point_y = geom.GetY()

    # Convert the point from meters to geom_x/long
    reference_point_latlng = coord_trans_opposite.TransformPoint(
        reference_point_x, reference_point_y)

    # Get the size of the pixels in meters, to be used for creating rasters
    pixel_xsize, pixel_ysize = _pixel_size_based_on_coordinate_transform(
        base_raster_path, coord_trans, reference_point_latlng)

    # # Average the pixel sizes in case they are of different sizes
    # mean_pixel_size = (abs(pixel_xsize) + abs(pixel_ysize)) / 2.0
    # pixel_size_tuple = (mean_pixel_size * numpy.sign(pixel_xsize),
    #                     mean_pixel_size * numpy.sign(pixel_ysize))

    feat = None
    layer = None
    vector = None

    return (pixel_xsize, pixel_ysize)


def _pixel_size_based_on_coordinate_transform(
        base_raster_path, coord_trans, reference_point):
    """Get width and height of cell in meters.

    Calculates the pixel width and height in meters given a coordinate
    transform and reference point on the dataset that's close to the
    transformed projected coordinate system. This is only necessary if raster
    is not already in a meter coordinate system, for example raster may be in
    lat/long (WGS84).

    Args:
        base_raster_path (str): path to a GDAL raster path, projected in the
            form of lat/long decimal degrees
        coord_trans (osr.CoordinateTransformation): an OSR coordinate
            transformation from dataset coordinate system to meters
        reference_point (tuple): a reference point close to the transformed
            coordinate system. Must be in the same coordinate system as
            the base raster.

    Returns:
        pixel_diff (tuple): a 2-tuple containing (pixel width in meters, pixel
            height in meters)

    """
    # Get the first points (x, y) from geoTransform
    geotransform = pygeoprocessing.get_raster_info(
        base_raster_path)['geotransform']
    pixel_size_x = geotransform[1]
    pixel_size_y = geotransform[5]
    top_left_x = reference_point[0]
    top_left_y = reference_point[1]
    # Create the second point by adding the pixel width/height
    new_x = top_left_x + pixel_size_x
    new_y = top_left_y + pixel_size_y
    # Transform two points into meters
    point_1 = coord_trans.TransformPoint(top_left_x, top_left_y)
    point_2 = coord_trans.TransformPoint(new_x, new_y)

    # Calculate the x/y difference between two points
    # taking the absolute value because the direction doesn't matter for pixel
    # size in the case of most coordinate systems where y increases up and x
    # increases to the right (right handed coordinate system).
    pixel_diff_x = point_2[0] - point_1[0]
    pixel_diff_y = point_2[1] - point_1[1]

    return (pixel_diff_x, pixel_diff_y)


def _create_raster_attr_table(base_raster_path, attr_dict, column_name):
    """Create a raster attribute table (RAT).

    Parameters:
        base_raster_path (str): a GDAL raster dataset to create the RAT for
        attr_dict (dict): a dictionary with keys that point to a primitive type
            ex: {integer_id_1: value_1, ... integer_id_n: value_n}
        column_name (str): a string for the column name that maps the values

    Returns:
        None

    """
    raster = gdal.OpenEx(base_raster_path, gdal.GA_Update)
    band = raster.GetRasterBand(1)
    attr_table = gdal.RasterAttributeTable()
    attr_table.SetRowCount(len(attr_dict))

    # create columns
    attr_table.CreateColumn('Value', gdal.GFT_Integer, gdal.GFU_MinMax)
    attr_table.CreateColumn(column_name, gdal.GFT_String, gdal.GFU_Name)

    row_count = 0
    for key in sorted(attr_dict.keys()):
        attr_table.SetValueAsInt(row_count, 0, int(key))
        attr_table.SetValueAsString(row_count, 1, attr_dict[key])
        row_count += 1

    band.SetDefaultRAT(attr_table)

    # Make sure the dataset is closed and cleaned up
    # gdal.Dataset.__swig_destroy__(raster)
    raster = None


@validation.invest_validator
def validate(args, limit_to=None):
    """Validate an input dictionary for Wave Energy.

    Parameters:
        args (dict): The args dictionary.
        limit_to=None (str or None): If a string key, only this args parameter
            will be validated.  If ``None``, all args parameters will be
            validated.

    Returns:
        warnings (list): A list of tuples where tuple[0] is an iterable of keys
            that the error message applies to and tuple[1] is the string
            validation warning.

    """
    warnings = []
    keys_missing_value = []
    missing_keys = []
    for required_key in ('workspace_dir', 'wave_base_data_path',
                         'analysis_area_path', 'machine_perf_path',
                         'machine_param_path', 'dem_path'):
        try:
            if args[required_key] in ('', None):
                keys_missing_value.append(required_key)
        except KeyError:
            missing_keys.append(required_key)

    if missing_keys > 0:
        raise KeyError('Keys are missing from args: %s' % str(missing_keys))

    if keys_missing_value > 0:
        warnings.append((keys_missing_value,
                         'Parameter is required but has no value'))

    if limit_to in ('wave_base_data_path', None):
        if not os.path.isdir(args['wave_base_data_path']):
            warnings.append((['wave_base_data_path'],
                             'Parameter not found or is not a folder.'))

    if limit_to in ('analysis_area_path', None):
        if args['analysis_area_path'] not in (
                "West Coast of North America and Hawaii",
                "East Coast of North America and Puerto Rico",
                "North Sea 4 meter resolution",
                "North Sea 10 meter resolution", "Australia", "Global"):
            warnings.append((['analysis_area_path'],
                             'Parameter must be a known analysis area.'))

    if limit_to in ('aoi_path', None):
        try:
            if args['aoi_path'] not in ('', None):
                with utils.capture_gdal_logging():
                    vector = gdal.OpenEx(args['aoi_path'])
                    layer = vector.GetLayer()
                    geometry_type = layer.GetGeomType()
                    if geometry_type != ogr.wkbPolygon:
                        warnings.append((['aoi_path'],
                                         'Vector must contain only polygons.'))
                    srs = layer.GetSpatialRef()
                    units = srs.GetLinearUnitsName().lower()
                    if units not in ('meter', 'metre'):
                        warnings.append((['aoi_path'],
                                         'Vector must be projected in meters'))

                    datum = srs.GetAttrValue('DATUM')
                    if datum != 'WGS_1984':
                        warnings.append(
                            (['aoi_path'],
                             'Vector must use the WGS_1984 datum.'))
                    layer = None
                    vector = None
        except KeyError:
            # Parameter is not required.
            pass

    for csv_key, required_fields in (
        ('machine_perf_path', set([])),
        ('machine_param_path', set(['name', 'value', 'note'])),
        ('land_gridPts_path', set
            (['id', 'type', 'lat', 'long', 'location'])),
        ('machine_econ_path', set(['name', 'value', 'note']))):
        try:
            _, missing_fields = _get_validated_dataframe(
                args[csv_key], required_fields)
            if missing_fields:
                warnings.append('CSV is missing columns: %s' % ', '.join(
                    sorted(missing_fields)))
        except KeyError:
            # Not all these are required inputs.
            pass
        except IOError:
            warnings.append(([csv_key], 'File not found.'))

    if limit_to in ('dem_path', None):
        with utils.capture_gdal_logging():
            raster = gdal.OpenEx(args['dem_path'])
        if raster is None:
            warnings.append(
                (['dem_path'],
                 ('Parameter must be a filepath to a GDAL-compatible '
                  'raster file.')))
        raster = None

    if limit_to in ('number_of_machines', None):
        try:
            num_machines = args['number_of_machines']
            if (int(float(num_machines)) != float(num_machines)
                    or float(num_machines) < 0):
                warnings.append((['number_of_machines'],
                                 'Parameter must be a positive integer.'))
        except KeyError:
            pass
        except ValueError:
            warnings.append((['number_of_machines'],
                             'Parameter must be a number.'))

    if limit_to is None:
        if 'valuation_container' in args:
            missing_keys = []
            keys_with_no_value = []
            for required_key in ('land_gridPts_path', 'machine_econ_path',
                                 'number_of_machines'):
                try:
                    if args[required_key] in ('', None):
                        keys_with_no_value.append(required_key)
                except KeyError:
                    missing_keys.append(required_key)

            if missing_keys:
                raise KeyError('Keys are missing: %s' % missing_keys)

            if keys_with_no_value:
                warnings.append((keys_with_no_value,
                                 'Parameter must have a value'))

    return warnings
