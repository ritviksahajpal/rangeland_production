import logging
import os
import csv
from itertools import product

import pygeoprocessing

from natcap.invest.coastal_blue_carbon.utilities.raster import Raster

logging.basicConfig(format='%(asctime)s %(name)-20s %(levelname)-8s \
%(message)s', level=logging.DEBUG, datefmt='%m/%d/%Y %H:%M:%S ')

LOGGER = logging.getLogger('natcap.invest.coastal_blue_carbon.preprocessor')


def execute(args):
    '''
    Args:
        workspace_dir (string): desc
        results_suffix (string): desc
        lulc_lookup_table (string): desc
        lulc_snapshot_list (list): desc

    Example Args::

        args = {
            'workspace_dir': 'path/to/workspace_dir/',
            'results_suffix': '',
            'lulc_lookup_table': 'path/to/lookup.csv',
            'lulc_snapshot_list': ['path/to/raster1', 'path/to/raster2', ...]
        }
    '''
    vars_dict = _get_inputs(args)
    vars_dict = _preprocess_data(vars_dict)
    _create_transition_table(vars_dict)


def _get_inputs(args):
    vars_dict = {}
    # ...
    vars_dict = _get_derivative_inputs(vars_dict)
    _validate_inputs(vars_dict)
    return vars_dict


def _get_derivative_inputs(vars_dict):
    # ...
    lulc_lookup_dict = pygeoprocessing.geoprocessing.get_lookup_from_csv(
        vars_dict['lulc_lookup_uri'], 'code')

    for code in lulc_lookup_dict.keys():
        sub_dict = lulc_lookup_dict[code]
        val = sub_dict['is_coastal_blue_carbon_habitat']
        sub_dict['is_coastal_blue_carbon_habitat'] = eval(val.capitalize())
        lulc_lookup_dict[code] = sub_dict

    code_to_lulc_dict = {key: lulc_lookup_dict[key][
        'lulc-class'] for key in lulc_lookup_dict.keys()}
    lulc_to_code_dict = {v: k for k, v in code_to_lulc_dict.items()}

    vars_dict['lulc_lookup_dict'] = lulc_lookup_dict
    vars_dict['code_to_lulc_dict'] = code_to_lulc_dict
    vars_dict['lulc_to_code_dict'] = lulc_to_code_dict

    return vars_dict


def _validate_inputs(vars_dict):
    lulc_snapshot_list = vars_dict['lulc_snapshot_list']
    lulc_lookup_dict = vars_dict['lulc_lookup_dict']

    # assert rasters aligned
    for snapshot_idx in range(0, len(lulc_snapshot_list)-1):
        raster1 = Raster.from_file(lulc_snapshot_list[snapshot_idx])
        raster2 = Raster.from_file(lulc_snapshot_list[snapshot_idx+1])

        try:
            assert(raster1.is_aligned(raster2))
        except:
            class MisalignedRasters(ValueError):
                def __init__(self, message):
                    self.message = message
            raise MisalignedRasters(
                "At least one raster is misaligned from the others")

    # assert all raster values in lookup table
    raster_val_set = set()
    for snapshot_idx in range(0, len(lulc_snapshot_list)):
        raster = Raster.from_file(lulc_snapshot_list[snapshot_idx])
        raster_val_set = raster_val_set.union(
            set(raster.get_band(1).flatten()))

    code_set = set(lulc_lookup_dict.keys())
    try:
        assert(not raster_val_set.difference(code_set))
    except:
        class NotAllClassesInLookupTable(ValueError):
            def __init__(self, message):
                self.message = message
        raise NotAllClassesInLookupTable(
            "At least one raster value is not in the lookup table")

    # assert workspace exists, if not, make directory
    if not os.path.isdir(vars_dict['workspace_dir']):
        try:
            os.makedirs(vars_dict['workspace_dir'])
        except:
            LOGGER.error("Cannot create Workspace Directory")
            raise OSError


def _preprocess_data(vars_dict):

    def _get_land_cover_transitions(raster_t1_uri, raster_t2_uri):
        raster_t1 = Raster.from_file(raster_t1_uri)
        raster_t2 = Raster.from_file(raster_t2_uri)

        band_t1 = raster_t1.get_band(1).data.flatten()
        band_t2 = raster_t2.get_band(1).data.flatten()

        transition_list = zip(band_t1, band_t2)
        transition_set = set(transition_list)

        return transition_set


    def _mark_transition_type(lookup_dict, transition_matrix_dict, lulc_from, lulc_to):
        if (bool(lookup_dict[lulc_from]['is_coastal_blue_carbon_habitat']) and
            bool(lookup_dict[lulc_to]['is_coastal_blue_carbon_habitat'])):
            # veg --> veg
            transition_matrix_dict[(lulc_from, lulc_to)] = 'accumulation'
        elif (not bool(lookup_dict[lulc_from]['is_coastal_blue_carbon_habitat']) and
            bool(lookup_dict[lulc_to]['is_coastal_blue_carbon_habitat'])):
            # non-veg --> veg
            transition_matrix_dict[(lulc_from, lulc_to)] = 'accumulation'
        elif (bool(lookup_dict[lulc_from]['is_coastal_blue_carbon_habitat']) and
            not bool(lookup_dict[lulc_to]['is_coastal_blue_carbon_habitat'])):
            # veg --> non-veg        
            transition_matrix_dict[(lulc_from, lulc_to)] = 'disturbance'
        elif (not bool(lookup_dict[lulc_from]['is_coastal_blue_carbon_habitat']) and
              not bool(lookup_dict[lulc_to]['is_coastal_blue_carbon_habitat'])):
            # non-veg --> non-veg
            transition_matrix_dict[(lulc_from, lulc_to)] = 'unchanged'
        else:
            raise Exception

        return transition_matrix_dict

    # Transition Matrix
    lulc_lookup_dict = vars_dict['lulc_lookup_dict']
    p = product(lulc_lookup_dict.keys(), repeat=2)

    transition_matrix_dict = {}
    for i in p:
        transition_matrix_dict[i] = ''

    # Determine Transitions and Directions
    lulc_snapshot_list = vars_dict['lulc_snapshot_list']
    for snapshot_idx in range(0, len(lulc_snapshot_list)-1):
        transition_set = _get_land_cover_transitions(
            lulc_snapshot_list[snapshot_idx],
            lulc_snapshot_list[snapshot_idx+1])
        for transition_tuple in transition_set:
            transition_matrix_dict = _mark_transition_type(
                lulc_lookup_dict,
                transition_matrix_dict,
                *transition_tuple)

    vars_dict['transition_matrix_dict'] = transition_matrix_dict

    return vars_dict


def _create_transition_table(vars_dict):
    '''creates a transition table representing the lulc transition effect on
    carbon emissions or sequestration.'''
    lulc_class_list = vars_dict['lulc_class_list']
    transition_matrix_dict = vars_dict['transition_matrix_dict']
    code_to_lulc_dict = vars_dict['code_to_lulc_dict']

    transition_by_lulc_class_dict = dict([(lulc_class, {}) for lulc_class in lulc_class_list])

    for transition in transition_matrix_dict.keys():
        top_dict = transition_by_lulc_class_dict[code_to_lulc_dict[transition[0]]]
        top_dict[code_to_lulc_dict[transition[1]]] = transition_matrix_dict[transition]
        transition_by_lulc_class_dict[code_to_lulc_dict[transition[0]]] = top_dict

    if vars_dict['results_suffix'] != '':
        fname = 'transition_' + vars_dict['results_suffix'] + '.csv'
    else:
        fname = 'transition.csv'
    fpath = os.path.join(vars_dict['workspace_dir'], 'outputs', fname)
    with open(fpath, 'wb') as csv_file:
        fieldnames = ['lulc-classes'] + lulc_class_list
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for lulc_class in transition_by_lulc_class_dict.keys():
            row = dict([('lulc-classes', lulc_class)] + transition_by_lulc_class_dict[lulc_class].items())
            writer.writerow(row)
