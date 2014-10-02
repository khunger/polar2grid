#!/usr/bin/env python
# encoding: utf-8
"""
CrIS EDR front end for polar2grid, which extracts band-pass slices of brightness temperature data.

:author:       Ray Garcia (rayg)
:contact:      rayg@ssec.wisc.edu
:organization: Space Science and Engineering Center (SSEC)
:copyright:    Copyright (c) 2013 University of Wisconsin SSEC. All rights reserved.
:date:         Mar 2013
:license:      GNU GPLv3

Note that Dual Regression products are indexed strangely:
  [in-track, cross-track] for 2D variables
  [level, in-track, cross-track] for 3D variables

Example:
[(u'CAPE', (84, 60)),
 (u'CO2_Amount', (84, 60)),
 (u'COT', (84, 60)),
 (u'CTP', (84, 60)),
 (u'CTT', (84, 60)),
 (u'Channel_Index', (7021,)),
 (u'CldEmis', (84, 60)),
 (u'Cmask', (84, 60)),
 (u'Dewpnt', (101, 84, 60)),
 (u'GDAS_RelHum', (101, 84, 60)),
 (u'GDAS_TAir', (101, 84, 60)),
 (u'H2OMMR', (101, 84, 60)),
 (u'H2Ohigh', (84, 60)),
 (u'H2Olow', (84, 60)),
 (u'H2Omid', (84, 60)),
 (u'Latitude', (84, 60)),
 (u'Lifted_Index', (84, 60)),
 (u'Longitude', (84, 60)),
 (u'O3VMR', (101, 84, 60)),
 (u'Plevs', (101,)),
 (u'Qflag1', (84, 60)),
 (u'Qflag2', (84, 60)),
 (u'Qflag3', (84, 60)),
 (u'RelHum', (101, 84, 60)),
 (u'SurfEmis', (8461, 84, 60)),
 (u'SurfEmis_Wavenumbers', (8461,)),
 (u'SurfPres', (84, 60)),
 (u'TAir', (101, 84, 60)),
 (u'TSurf', (84, 60)),
 (u'totH2O', (84, 60)),
 (u'totO3', (84, 60))]


Copyright (C) 2013 Space Science and Engineering Center (SSEC),
 University of Wisconsin-Madison.

   This program is free software: you can redistribute it and/or modify
   it under the terms of the GNU General Public License as published by
   the Free Software Foundation, either version 3 of the License, or
   (at your option) any later version.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License for more details.

   You should have received a copy of the GNU General Public License
   along with this program.  If not, see <http://www.gnu.org/licenses/>.

This file is part of the polar2grid software package. Polar2grid takes
satellite observation data, remaps it, and writes it to a file format for
input into another program.
Documentation: http://www.ssec.wisc.edu/software/polar2grid/
"""

__docformat__ = "restructuredtext en"

import h5py
import numpy as np
import os
import sys
import logging
import re
import uuid
from datetime import datetime
from collections import namedtuple
from functools import partial
from pprint import pformat
from scipy import interpolate

from polar2grid.core.roles import FrontendRole
from polar2grid.core.fbf import str_to_dtype
from polar2grid.core.constants import *

LOG = logging.getLogger(__name__)

# Reliably chop filenames into identifying pieces
# e.g. IASI_d20130310_t152624_M02.atm_prof_rtv.h5
RE_DRRTV = re.compile(r'(?P<inst>[A-Za-z0-9]+)_d(?P<date>\d+)_t(?P<start_time>\d+)(?:_(?P<sat>[A-Za-z0-9]+))?.*?\.h5')

# whether or not to interpolate data to an exploded swath
DEFAULT_EXPLODE_SAMPLING = False
EXPLODE_FACTOR = 64

# GUIDEBOOK
# FUTURE: move this to another file when it grows large enough
# this table converts filename components to polar2grid identifiers (satellite, instrument, scan-line-grouping)
# scan-line grouping is significant to MS2GT components
# (sat, inst) => (p2g_sat, p2g_inst, rows_per_swath)
SAT_INST_TABLE = {
    (None, 'CRIS'): (SAT_NPP, INST_CRIS, 3, CRIS_NAV_UID),
    (None, 'CrIS'): (SAT_NPP, INST_CRIS, 3, CRIS_NAV_UID),
    # FIXME: this should be reviewed; consider how to fold instrument attributes into back-ends
    #        then figure out best way to handle conf tables referring to instrument name
    ('M02', 'IASI'): (SAT_METOPA, INST_IASI, 1, IASI_NAV_UID),
    ('M01', 'IASI'): (SAT_METOPB, INST_IASI, 1, IASI_NAV_UID),
    # ('g195', 'AIRS'): (None, None, 0),  # FIXME this needs work
    (None, 'AIRS'): (SAT_AQUA, INST_AIRS, 1, AIRS_NAV_UID),  # FIXME
}

# pressure layers to obtain data from
DEFAULT_LAYER_PRESSURES = (500.0, 900.0)

# h5_var_name => dkind, bkind, pressure-layers-or-None
VAR_TABLE = {
     'CAPE': (DKIND_CAPE, BKIND_CAPE, None),
     'CO2_Amount': (DKIND_CO2_AMOUNT, BKIND_CO2_AMT, None),
     'COT': (DKIND_OPTICAL_THICKNESS, BKIND_COT, None),
     'CTP': (DKIND_PRESSURE, BKIND_CTP, None),  # !
     'CTT': (DKIND_TEMPERATURE, BKIND_CTT, None),
     # 'Channel_Index': (None, ),
     'CldEmis': (DKIND_EMISSIVITY, BKIND_CLD_EMIS, None),
     'Cmask': (DKIND_CATEGORY, BKIND_CMASK, None),
     'Dewpnt': (DKIND_TEMPERATURE, BKIND_DEWPT, DEFAULT_LAYER_PRESSURES),
     # 'GDAS_RelHum': (DKIND_PERCENT, BKIND_RH),
     # 'GDAS_TAir': (DKIND_TEMPERATURE, BKIND_AIR_T),
     'H2OMMR': (DKIND_MIXING_RATIO, BKIND_H2O_MMR, DEFAULT_LAYER_PRESSURES),
     # 'H2Ohigh': (None, None, None),
     # 'H2Olow': (None, None, None),
     # 'H2Omid': (None, None, None),
     # 'Latitude': (DKIND_LATITUDE, None),
     'Lifted_Index': (DKIND_TEMPERATURE, BKIND_LI, None),# is in centigrade
     # 'Longitude': (DKIND_LONGITUDE, None),
     'O3VMR': (DKIND_MIXING_RATIO, BKIND_O3_VMR, DEFAULT_LAYER_PRESSURES),
     # 'Plevs': (DKIND_PRESSURE, None),
     # 'Qflag1': (None, None),
     # 'Qflag2': (None, None),
     # 'Qflag3': (None, None),
     'RelHum': (DKIND_PERCENT, BKIND_RH, DEFAULT_LAYER_PRESSURES),  # !
     # 'SurfEmis': (DKIND_EMISSIVITY, BKIND_SRF_EMIS, None),
     # 'SurfEmis_Wavenumbers': (None, None),
     'SurfPres': (DKIND_PRESSURE, BKIND_SRF_P, None),
     'TAir': (DKIND_TEMPERATURE, BKIND_AIR_T, DEFAULT_LAYER_PRESSURES),  # !
     'TSurf': (DKIND_TEMPERATURE, BKIND_SRF_T, None),
     'totH2O': (DKIND_TOTAL_WATER, BKIND_H2O_TOT, None),
     'totO3': (DKIND_TOTAL_OZONE, BKIND_O3_TOT, None),
     }


# END GUIDEBOOK


def _filename_info(pathname):
    """
    return a dictionary of metadata found in the filename
    :param pathname: dual retrieval HDF output file path
    :return: dictionary of polar2grid information found in the filename, or None if the file cannot be used
    """
    m = RE_DRRTV.match(os.path.split(pathname)[-1])
    if not m:
        LOG.debug('%s doesn\'t match DR-RTV file naming convention' % pathname)
        return None
    mgd = m.groupdict()
    when = datetime.strptime('%(date)s %(start_time)s' % mgd, '%Y%m%d %H%M%S')
    # fetch with preference toward satellite matching - failing that, check with sat=None case
    sat, inst, rps, nav_set_uid = SAT_INST_TABLE.get((mgd['sat'], mgd['inst']),
                                        SAT_INST_TABLE.get((None, mgd['inst'])))
    return { 'start_time': when,
             'nav_set_uid': nav_set_uid,
             'sat': sat,
             'instrument': inst,    # why is this not 'inst'? or 'sat' 'satellite'?
             'rows_per_scan': rps
             }

def _swath_shape(*h5s):
    """
    determine the shape of the retrieval swath
    :param h5s: list of hdf5 objects
    :return: (layers, rows, cols)
    """
    layers, rows, cols = 0, 0, 0
    for h5 in h5s:
        rh = h5['RelHum']
        l, r, c = rh.shape
        if layers == 0:
            layers = l
        if cols == 0:
            cols = c
        rows += r
    return layers, rows, cols


def _swath_info(*h5s):
    """
    return FrontEnd metadata found in attributes
    :param h5s: hdf5 object list
    :return: dictionary of metadata extracted from attributes, including extra '_plev' pressure variable
    """
    fn_info = _filename_info(h5s[0].filename)
    LOG.debug(repr(fn_info))
    layers, rows, cols = _swath_shape(*h5s)
    rps = fn_info['rows_per_scan']
    # fn_info['rows_per_scan'] = rows
    zult = {'swath_rows': rows,
            'swath_cols': cols,
            'swath_scans': rows / rps,
            '_plev': h5s[0]['Plevs'][:].squeeze()
            }
    zult.update(fn_info)
    return zult


def _explode(data, factor):
    rows,cols = data.shape
    r = np.arange(rows, dtype=np.float64)
    c = np.arange(cols, dtype=np.float64)
    rr = np.linspace(0.0, float(rows-1), rows*factor)
    cc = np.linspace(0.0, float(cols-1), cols*factor)
    spl = interpolate.RectBivariateSpline(r, c, data, kx=1, ky=1)
    return spl(rr,cc).astype(np.float32)


def _make_longitude_monotonic(lon_swath):
    """
    Modify longitude in place to be monotonic -180..180 or 0..360
    :param lon_swath: 2D numpy masked_array of longitude data
    :return: modified array
    """
    rows,cols = lon_swath.shape
    shift = False
    for r in range(rows):
        dif = np.abs(np.diff(lon_swath[r,:].squeeze()))
        if np.max(dif) > 180.0:
            shift = True
            break
    if shift:
        lon_swath[lon_swath < 0] += 360.0
    return lon_swath


def _swath_from_var(var_name, h5_var, tool=None):
    """
    given a variable by name, and its hdf5 variable object,
    return a normalized numpy masked_array with corrected indexing order
    :param var_name: variable name, used to consult internal guidebook
    :param h5_var: hdf5 object
    :return: numpy masked_array with missing data properly masked and dimensions corrected to
            [in-track, cross-track, layer] for 3D variables
    """
    if tool is not None:
        data = tool(h5_var)
    else:
        data = h5_var[:]
    shape = data.shape

    if len(shape) == 3:
        # roll the layer axis to the back, eg (101, 84, 60) -> (84, 60, 101)
        LOG.debug('rolling %s layer axis to last position' % var_name)
        data = np.rollaxis(data, 0, 3)

    if 'missing_value' in h5_var.attrs:
        mv = float(h5_var.attrs['missing_value'][0])
        LOG.debug('missing value for %s is %s' % (var_name, mv))
        mask = np.abs(data - mv) < 0.5
        data[mask] = DEFAULT_FILL_VALUE # FUTURE: we'd rather just deal with masked_array properly in output layer
        data = np.ma.masked_array(data, mask)  # FUTURE: convince scientists to use NaN. also world peace
        LOG.debug('min, max = %s, %s' % (np.min(data.flatten()), np.max(data.flatten())))
    else:
        LOG.warning('no missing_value attribute in %s' % var_name)
        data = np.ma.masked_array(data)

    return data


def _dict_reverse(D):
    return dict((v,k) for (k,v) in D.items())


nptype_to_suffix = _dict_reverse(str_to_dtype)


def _write_array_to_fbf(name, data):
    """
    write a swath to a flat binary file, including mapping missing values to DEFAULT_FILL_VALUE
    :param name: variable name
    :param data: data array, typically a numpy.masked_array
    :return:
    """
    if len(data.shape) != 2:
        LOG.warning('data %r shape is %r, ignoring' % (name, data.shape))
        return None
    if hasattr(data, 'mask'):
        mask = data.mask
        LOG.debug('found mask for %s' % name)
        data = np.array(data, dtype=np.float32)
        data[mask] = DEFAULT_FILL_VALUE
    rows, cols = data.shape
    dts = nptype_to_suffix[data.dtype.type]
    suffix = '.%s.%d.%d' % (dts, cols, rows)
    fn = name + suffix
    LOG.debug('writing to %s...' % fn)
    if data.dtype != np.float32:
        data = data.astype(np.float32)
    with file(fn, 'wb') as fp:
        data.tofile(fp)
    return fn


def _layer_at_pressure(h5v, plev=None, p=None):
    """
    extract a layer of a variable assuming (layer, rows, cols) indexing and plev lists layer pressures
    this is used to construct slicing tools that are built into the manifest list
    :param h5v: hdf5 variable object
    :param plev: pressure array corresponding to layer dimension
    :param p: pressure level value to find
    :return: data slice from h5v
    """
    # dex = np.searchsorted(plev, p)
    dex = np.abs(plev - p).argmin()   # FUTURE: memo-ize this value since it shouldn't vary for DR-RTV files

    try:
        LOG.debug('using level %d=%f near %r as %f' % (dex, plev[dex], plev[dex-1:dex+2], p))
    except IndexError:
        pass
    return h5v[dex,:]


# tuple describing what swath data we want from a given input series
manifest_entry = namedtuple("manifest_entry", 'h5_var_name tool bkind dkind bid')


def _var_manifest(sat, inst, plev):
    """
    return set of variable extraction info given satellite, instrument pair, its manifest destiny
    :param sat: const SAT_NPP, SAT_METOPA, etc
    :param inst: INST_IASI, INST_CRIS
    :param plev: pressure level array assumed consistent between files
    :return: yields sequence of (variable-name, manifest-entry-tuple)
    """
    # FIXME: this is not fully implemented and needs to use the guidebook as well as generate layer extraction tools

    for h5_var_name, info in VAR_TABLE.items():
        dk, bk, ps = info
        if ps:
            for p in ps:
                yield '%s_%dmb' % (h5_var_name, p), manifest_entry(h5_var_name=h5_var_name,
                                                                   tool=partial(_layer_at_pressure, plev=plev, p=p),
                                                                   dkind=dk,
                                                                   bkind=bk,
                                                                   bid='lvl%d' % int(p))
        else:
            yield h5_var_name, manifest_entry(h5_var_name=h5_var_name,
                                              tool=None,
                                              dkind=dk,
                                              bkind=bk,
                                              bid=None)


    # FIXME: final code should look something like this:
    # for h5_var_name, info in VAR_TABLE.items():
    #     dk, bk, ps = info
    #     if not ps:
    #         if dk is None or bk is None:
    #             continue
    #         yield h5_var_name, manifest_entry(h5_var_name=h5_var_name,
    #                                           tool=None,  # 2D variable, take the whole variable
    #                                           dkind=dk,
    #                                           bkind=bk,
    #                                           bid=BID_NONE)  # FIXME this should be based on level
    #     else:
    #         for p in ps:
    #             yield '%s_%dmb' % (h5_var_name, p), manifest_entry(h5_var_name=h5_var_name,
    #                                                                tool=partial(_layer_at_pressure, plev=plev, p=p),
    #                                                                dkind=dk,
    #                                                                bkind=bk,
    #                                                                bid=None)  # FIXME this should be based on level



def swathbuckler(*h5_pathnames):
    """
    return swath metadata after reading all the files in and writing out fbf files
    :param h5_pathnames:
    :return: fully formed metadata describing swath written to current working directory
    """
    h5_pathnames = list(h5_pathnames)

    bad_files = set()
    for pn in h5_pathnames:
        if not h5py.is_hdf5(pn):
            LOG.warning('%s is not a proper HDF5 file' % pn)
            bad_files.add(pn)
        if not RE_DRRTV.match(os.path.split(pn)[-1]):
            bad_files.add(pn)
    if bad_files:
        LOG.warning('These files are not usable and will be ignored: %s' % ', '.join(list(bad_files)))
        h5_pathnames = [x for x in h5_pathnames if x not in bad_files]

    h5s = [h5py.File(pn, 'r') for pn in h5_pathnames if pn not in bad_files]
    if not h5s:
        LOG.error('no input was available to process!')
        return {}
    nfo = _swath_info(*h5s)
    bands = nfo['bands'] = {}
    # get list of output "bands", their characteristics, and an extraction tool
    manifest = dict(_var_manifest(nfo['sat'], nfo['instrument'], nfo['_plev']))
    LOG.debug('manifest to extract: %s' % pformat(manifest))

    def _gobble(name, h5_var_name, tool, h5s=h5s, explode=DEFAULT_EXPLODE_SAMPLING, filter=None):
        "extract a swath to a FBF file and return the path"
        sections = [_swath_from_var(h5_var_name, h5[h5_var_name], tool) for h5 in h5s]
        swath = np.concatenate(sections, axis=0)
        swarthy = swath if filter is None else filter(swath)
        arr_a_pirate = _explode(swarthy, EXPLODE_FACTOR) if explode else swath
        return _write_array_to_fbf(name, arr_a_pirate)

    nfo['fbf_lat'] = _gobble('swath_latitude', 'Latitude', None)
    nfo['fbf_lon'] = _gobble('swath_longitude', 'Longitude', None) #, filter=_make_longitude_monotonic)

    if DEFAULT_EXPLODE_SAMPLING:
        nfo['rows_per_scan'] *= EXPLODE_FACTOR
        nfo['swath_rows'] *= EXPLODE_FACTOR
        nfo['swath_cols'] *= EXPLODE_FACTOR

    # extract swaths and generate downstream metadata
    for name, guide in manifest.items():
        LOG.debug("extracting %s from variable %s" % (name, guide.h5_var_name))
        filename = _gobble(name, guide.h5_var_name, guide.tool)
        band = {
            "band": guide.bid,
            "data_kind": guide.dkind,
            "remap_data_as": guide.dkind,
            "kind": guide.bkind,
            "fbf_img": filename,
            "swath_rows": nfo['swath_rows'],
            "swath_cols": nfo['swath_cols'],
            "swath_scans": nfo['swath_scans'],
            "rows_per_scan": nfo['rows_per_scan'],
            "grids": GRIDS_ANY
        }
        # bands[name] = band
        bands[(guide.bkind, guide.bid)] = band
    #nfo['nav_set_uid'] = 'cris_nav'   # FIXME move nav_set to the guidebook
    LOG.debug('metadata: %s' % pformat(nfo))
    return nfo


class Frontend(FrontendRole):
    """
    """
    removable_file_patterns = [
        'CAPE.real4.*',
        'CO*.real4.*',
        'CT*.real4.*',
        'CldEmis.real4.*',
        'Cmask.real4.*',
        'Dewpnt_*.real4.*',
        'H2OMMR_*.real4.*',
        'Lifted_Index.real4.*',
        'O3VMR_*.real4.*',
        'RelHum_*.real4.*',
        'SurfPres.real4.*',
        'TAir_*.real4.*',
        'TSurf.real4.*',
        'tot*.real4.*',
        'swath_longitude.real4.*',
        'swath_latitude.real4.*'
    ]
    info = None

    def __init__(self, **kwargs):
        self.info = {}

    @classmethod
    def parse_datetimes_from_filepaths(cls, filepaths):
        zult = []
        for pn in filepaths:
            nfo = _filename_info(pn)
            if not nfo:
                continue
            zult.append(nfo['start_time'] if nfo is not None else None)
        return zult

    @classmethod
    def sort_files_by_nav_uid(cls, filepaths):
        ret = {}
        for pn in filepaths:
            file_info = _filename_info(pn)
            if not file_info:
                continue
            if file_info["nav_set_uid"] not in ret:
                ret[file_info["nav_set_uid"]] = []
            ret[file_info["nav_set_uid"]].append(pn)

        return ret

    def make_swaths(self, filepaths, **kwargs):
        """
        load the swath from the input dir/files
        extract BT slices
        write BT slices to flat files in cwd
        write GEO arrays to flat files in cwd
        """
        self.info = swathbuckler(*filepaths)
        return self.info


# def test_swath(test_data='test/input/case1/IASI_d20130310_t152624_M02.atm_prof_rtv.h5'):
#     swath = swaths_from_h5s([test_data])
#     return swath


# def test_frontend(test_data='test/input/case1/IASI_d20130310_t152624_M02.atm_prof_rtv.h5'):
#     fe = CrisSdrFrontend()
#     fe.make_swaths([test_data])


def console(banner="enjoy delicious ipython"):
    from IPython.config.loader import Config
    cfg = Config()
    prompt_config = cfg.PromptManager
    prompt_config.in_template = 'In <\\#>: '
    prompt_config.in2_template = '   .\\D.: '
    prompt_config.out_template = 'Out<\\#>: '

    # First import the embeddable shell class
    from IPython.frontend.terminal.embed import InteractiveShellEmbed

    # Now create an instance of the embeddable shell. The first argument is a
    # string with options exactly as you would type them if you were starting
    # IPython at the system command line. Any parameters you want to define for
    # configuration can thus be specified here.
    ipshell = InteractiveShellEmbed(config=cfg,
                           banner1 = 'Welcome to IPython\n%s\n' % banner,
                           exit_msg = 'Leaving Interpreter, buh-bye.')
    ipshell()



def main():
    import optparse
    usage = """
%prog [options] ...

"""
    parser = optparse.OptionParser(usage)
    parser.add_option('-t', '--test', dest="self_test",
                    action="store_true", default=False, help="run self-tests")
    parser.add_option('-I', '--interactive', dest="interactive",
                    action="store_true", default=False, help="create swaths and interact with metadata")
    parser.add_option('-v', '--verbose', dest='verbosity', action="count", default=0,
                    help='each occurrence increases verbosity 1 level through ERROR-WARNING-INFO-DEBUG')
    # parser.add_option('-o', '--output', dest='output', default='.',
    #                  help='directory in which to store output')
    # # parser.add_option('-F', '--format', dest='format', default=DEFAULT_PNG_FMT,
    #                  help='format string for output filenames')
    # parser.add_option('-L', '--label', dest='label', default=DEFAULT_LABEL_FMT,
    #                  help='format string for labels')

    (options, args) = parser.parse_args()

    # FUTURE: validating the format strings is advisable

    # make options a globally accessible structure, e.g. OPTS.
    global OPTS
    OPTS = options

    if options.self_test:
        import doctest
        doctest.testmod()
        sys.exit(2)

    levels = [logging.ERROR, logging.WARN, logging.INFO, logging.DEBUG]
    logging.basicConfig(level=levels[min(3, options.verbosity)])

    if not args:
        parser.error('incorrect arguments, try -h or --help.')
        return 9

    meta = swathbuckler(*args)
    if options.interactive:
        global m
        m = meta
        console("'m' is metadata")
    else:
        from pprint import pprint
        pprint(meta)

    return 0


if __name__ == '__main__':
    sys.exit(main())
