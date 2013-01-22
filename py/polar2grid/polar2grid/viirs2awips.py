#!/usr/bin/env python
# encoding: utf-8
"""Script that uses the `polar2grid` toolbox of modules to take VIIRS
hdf5 (.h5) files and create a properly scaled AWIPS compatible NetCDF file.

:author:       David Hoese (davidh)
:contact:      david.hoese@ssec.wisc.edu
:organization: Space Science and Engineering Center (SSEC)
:copyright:    Copyright (c) 2013 University of Wisconsin SSEC. All rights reserved.
:date:         Jan 2013
:license:      GNU GPLv3

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

    Written by David Hoese    January 2013
    University of Wisconsin-Madison 
    Space Science and Engineering Center
    1225 West Dayton Street
    Madison, WI  53706
    david.hoese@ssec.wisc.edu

"""
__docformat__ = "restructuredtext en"

from polar2grid.core import Workspace
from polar2grid.core.glue_utils import setup_logging,create_exc_handler,remove_file_patterns
from polar2grid.core.constants import *
from polar2grid.viirs import Frontend
from .grids.grids import create_grid_jobs
import remap
from .awips import Backend

import os
import sys
import logging
from multiprocessing import Process

log = logging.getLogger(__name__)
GLUE_NAME = "viirs2awips"
LOG_FN = os.environ.get("VIIRS2AWIPS_LOG", "./%s.log" % (GLUE_NAME,))

def process_data_sets(filepaths,
        fornav_D=None, fornav_d=None,
        forced_grid=None,
        forced_gpd=None, forced_nc=None,
        create_pseudo=True,
        num_procs=1,
        rescale_config=None,
        backend_config=None,
        new_dnb=False # XXX
        ):
    """Process all the files provided from start to finish,
    from filename to AWIPS NC file.
    """
    status_to_return = STATUS_SUCCESS

    # Declare polar2grid components
    frontend = Frontend()
    backend = Backend(
            rescale_config=rescale_config,
            backend_config=backend_config
            )

    # Extract Swaths
    log.info("Extracting swaths...")
    try:
        meta_data = frontend.make_swaths(
                filepaths,
                scale_dnb=True,
                new_dnb=new_dnb,
                create_fog=create_pseudo,
                cut_bad=True
                )

        # Let's be lazy and give names to the 'global' viirs info
        sat = meta_data["sat"]
        instrument = meta_data["instrument"]
        start_time = meta_data["start_time"]
        bands = meta_data["bands"]
        fbf_lat = meta_data["fbf_lat"]
        fbf_lon = meta_data["fbf_lon"]
        nav_set_uid = meta_data["nav_set_uid"]
    except StandardError:
        log.error("Swath creation failed")
        log.debug("Swath creation error:", exc_info=1)
        status_to_return |= STATUS_FRONTEND_FAIL
        return status_to_return

    if len(bands) == 0:
        log.error("No more bands to process, quitting...")
        return status_to_return or STATUS_UNKNOWN_FAIL

    # Determine grid
    try:
        log.info("Determining what grids the data fits in...")
        grid_jobs = create_grid_jobs(sat, instrument, bands, fbf_lat, fbf_lon, backend,
                forced_grids=forced_grid)
    except StandardError:
        log.debug("Grid Determination error:", exc_info=1)
        log.error("Determining data's grids failed")
        status_to_return |= STATUS_GDETER_FAIL
        return status_to_return

    ### Remap the data
    try:
        remapped_jobs = remap.remap_bands(sat, instrument, nav_set_uid,
                fbf_lon, fbf_lat, grid_jobs,
                num_procs=num_procs, fornav_d=fornav_d, fornav_D=fornav_D,
                lat_fill_value=meta_data.get("lat_fill_value", None),
                lon_fill_value=meta_data.get("lon_fill_value", None),
                lat_south=meta_data.get("lat_south", None),
                lat_north=meta_data.get("lat_north", None),
                lon_west=meta_data.get("lon_west", None),
                lon_east=meta_data.get("lon_east", None)
                )
    except StandardError:
        log.debug("Remapping Error:", exc_info=1)
        log.error("Remapping data failed")
        status_to_return |= STATUS_REMAP_FAIL
        return status_to_return

    ### BACKEND ###
    W = Workspace('.')
    for grid_name,grid_dict in remapped_jobs.items():
        for (band_kind, band_id),band_dict in grid_dict.items():
            log.info("Running AWIPS backend for %s%s band grid %s" % (band_kind, band_id, grid_name))
            try:
                # Get the data from the flat binary file
                data = getattr(W, band_dict["fbf_remapped"].split(".")[0]).copy()

                # Call the backend
                backend.create_product(
                        sat,
                        instrument,
                        band_kind,
                        band_id,
                        band_dict["data_kind"],
                        data,
                        start_time=start_time,
                        grid_name=grid_name,
                        ncml_template=forced_nc or None,
                        fill_value=band_dict.get("fill_value", None)
                        )
            except StandardError:
                log.error("Error in the AWIPS backend for %s%s in grid %s" % (band_kind, band_id, grid_name))
                log.debug("AWIPS backend error:", exc_info=1)
                del remapped_jobs[grid_name][(band_kind, band_id)]

        if len(remapped_jobs[grid_name]) == 0:
            log.error("All backend jobs for grid %s failed" % (grid_name,))
            del remapped_jobs[grid_name]

    if len(remapped_jobs) == 0:
        log.warning("AWIPS backend failed for all grids for bands %r" % (bands.keys(),))
        status_to_return |= STATUS_BACKEND_FAIL

    log.info("Processing of bands %r is complete" % (bands.keys(),))

    return status_to_return

def _process_data_sets(*args, **kwargs):
    """Wrapper function around `process_data_sets` so that it can called
    properly from `run_glue`, where the exitcode is the actual
    returned value from `process_data_sets`.

    This function also checks for exceptions other than the ones already
    checked for in `process_data_sets` so that they are
    recorded properly.
    """
    try:
        stat = process_data_sets(*args, **kwargs)
        sys.exit(stat)
    except MemoryError:
        log.error("%s ran out of memory, check log file for more info" % (GLUE_NAME,))
        log.debug("Memory error:", exc_info=1)
    except OSError:
        log.error("%s had a OS error, check log file for more info" % (GLUE_NAME,))
        log.debug("OS error:", exc_info=1)
    except StandardError:
        log.error("%s had an unexpected error, check log file for more info" % (GLUE_NAME,))
        log.debug("Unexpected/Uncaught error:", exc_info=1)
    except KeyboardInterrupt:
        log.info("%s was cancelled by a keyboard interrupt" % (GLUE_NAME,))

    sys.exit(-1)

def run_glue(filepaths,
        multiprocess=True, **kwargs
        ):
    """Separate input files into groups that share navigation files data.

    Call the processing function in separate process or same process depending
    on value of `multiprocess` keyword.
    """
    # Rewrite/force parameters to specific format
    filepaths = [ os.path.abspath(os.path.expanduser(x)) for x in sorted(filepaths) ]

    M_files = sorted(set([ x for x in filepaths if os.path.split(x)[1].startswith("SVM") ]))
    I_files = sorted(set([ x for x in filepaths if os.path.split(x)[1].startswith("SVI") ]))
    DNB_files = sorted(set([ x for x in filepaths if os.path.split(x)[1].startswith("SVDNB") ]))
    all_used = set(M_files + I_files + DNB_files)
    all_provided = set(filepaths)
    not_used = all_provided - all_used
    if len(not_used):
        log.warning("Didn't know what to do with\n%s" % "\n".join(list(not_used)))

    pM = None
    pI = None
    pDNB = None
    exit_status = 0
    if len(M_files) != 0:
        log.debug("Processing M files")
        try:
            if multiprocess:
                pM = Process(target=_process_data_sets,
                        args = (M_files,),
                        kwargs = kwargs
                        )
                pM.start()
            else:
                stat = _process_data_sets(M_files, **kwargs)
                exit_status = exit_status or stat
        except StandardError:
            log.error("Could not process M files")
            exit_status = exit_status or len(M_files)

    if len(I_files) != 0:
        log.debug("Processing I files")
        try:
            if multiprocess:
                pI = Process(target=_process_data_sets,
                        args = (I_files,),
                        kwargs = kwargs
                        )
                pI.start()
            else:
                stat = _process_data_sets(I_files, **kwargs)
                exit_status = exit_status or stat
        except StandardError:
            log.error("Could not process I files")
            exit_status = exit_status or len(I_files)

    if len(DNB_files) != 0:
        log.debug("Processing DNB files")
        try:
            if multiprocess:
                pDNB = Process(target=_process_data_sets,
                        args = (DNB_files,),
                        kwargs = kwargs
                        )
                pDNB.start()
            else:
                stat = _process_data_sets(DNB_files, **kwargs)
                exit_status = exit_status or stat
        except StandardError:
            log.error("Could not process DNB files")
            exit_status = exit_status or len(DNB_files)

    log.debug("Waiting for subprocesses")
    if pM is not None:
        pM.join()
        stat = pM.exitcode
        exit_status = exit_status or stat
    if pI is not None:
        pI.join()
        stat = pI.exitcode
        exit_status = exit_status or stat
    if pDNB is not None:
        pDNB.join()
        stat = pDNB.exitcode
        exit_status = exit_status or stat

    return exit_status

def main(argv = sys.argv[1:]):
    import argparse
    description = """
    Create VIIRS swaths, remap them to a grid, and place that remapped data
    into a AWIPS compatible netcdf file.
    """
    parser = argparse.ArgumentParser(description=description)

    parser.add_argument('-v', '--verbose', dest='verbosity', action="count", default=0,
            help='each occurrence increases verbosity 1 level through ERROR-WARNING-INFO-DEBUG (default INFO)')
    parser.add_argument('--fornav-D', dest='fornav_D', default=40,
            help="Specify the -D option for fornav")
    parser.add_argument('--fornav-d', dest='fornav_d', default=2,
            help="Specify the -d option for fornav")
    parser.add_argument('--sp', dest='single_process', default=False, action='store_true',
            help="Processing is sequential instead of one process per kind of band")
    parser.add_argument('--num-procs', dest="num_procs", default=1,
            help="Specify number of processes that can be used to run ll2cr/fornav calls in parallel")
    parser.add_argument('--no-pseudo', dest='create_pseudo', default=True, action='store_false',
            help="Don't create pseudo bands")
    parser.add_argument('--new-dnb', dest='new_dnb', default=False, action='store_true',
            help="run new DNB scaling if provided DNB data (temporary)") # XXX

    # Remapping/Grids
    parser.add_argument('-g', '--grids', dest='forced_grids', nargs="+", default="all",
            help="Force remapping to only some grids, defaults to 'all', use 'all' for determination")
    parser.add_argument('--gpd', dest='forced_gpd', default=None,
            help="Specify a different gpd file to use")

    # Backend Specific
    parser.add_argument('--nc', dest='forced_nc', default=None,
            help="Specify a different ncml file to use")
    parser.add_argument('--backend-config', dest='backend_config', default=None,
            help="specify alternate backend configuration file")
    parser.add_argument('--rescale-config', dest='rescale_config', default=None,
            help="specify alternate rescale configuration file")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('-f', dest='data_files', nargs="+",
            help="List of one or more hdf files")
    group.add_argument('-d', dest='data_dir', nargs="?",
            help="Data directory to look for input data files")
    group.add_argument('-R', dest='remove_prev', default=False, action='store_true',
            help="Delete any files that may conflict with future processing. Processing is not done with this flag.")

    args = parser.parse_args(args=argv)

    levels = [logging.ERROR, logging.WARN, logging.INFO, logging.DEBUG]
    setup_logging(console_level=levels[min(3, args.verbosity)], log_filename=LOG_FN)

    # Don't set this up until after you have setup logging
    sys.excepthook = create_exc_handler(GLUE_NAME)

    fornav_D = int(args.fornav_D)
    fornav_d = int(args.fornav_d)
    num_procs = int(args.num_procs)
    forced_grids = args.forced_grids
    if forced_grids == 'all': forced_grids = None
    if args.forced_gpd is not None:
        args.forced_gpd = os.path.realpath(os.path.expanduser(args.forced_gpd))
        if not os.path.exists(args.forced_gpd):
            log.error("Specified gpd file does not exist '%s'" % args.forced_gpd)
            return -1
    if args.forced_nc is not None:
        args.forced_nc = os.path.realpath(os.path.expanduser(args.forced_nc))
        if not os.path.exists(args.forced_nc):
            log.error("Specified nc file does not exist '%s'" % args.forced_nc)
            return -1

    if args.remove_prev:
        log.info("Removing any possible conflicting files")
        remove_file_patterns(
                Frontend.removable_file_patterns,
                remap.removable_file_patterns,
                Backend.removable_file_patterns
                )
        return 0

    if args.data_files:
        hdf_files = args.data_files[:]
    elif args.data_dir:
        base_dir = os.path.abspath(os.path.expanduser(args.data_dir))
        hdf_files = [ os.path.join(base_dir,x) for x in os.listdir(base_dir) if x.startswith("SV") and x.endswith(".h5") ]
    else:
        log.error("Wrong number of arguments")
        parser.print_help()
        return -1
    # Handle the user using a '~' for their home directory
    hdf_files = [ os.path.realpath(os.path.expanduser(x)) for x in hdf_files ]

    stat = run_glue(hdf_files, fornav_D=fornav_D, fornav_d=fornav_d,
                forced_grid=forced_grids,
                forced_gpd=args.forced_gpd, forced_nc=args.forced_nc,
                create_pseudo=args.create_pseudo,
                multiprocess=not args.single_process, num_procs=num_procs,
                rescale_config=args.rescale_config,
                backend_config=args.backend_config,
                new_dnb=args.new_dnb # XXX
                )

    return stat

if __name__ == "__main__":
    sys.exit(main())

