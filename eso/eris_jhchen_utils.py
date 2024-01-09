#!/usr/bin/env python

"""
Authors: Jianhang Chen

This program was initially written when I learnt how to analysis the ESO/ERIS data for the first
time. 

History:
    - 2023-11-22: first release, v0.1
    - 2023-11-28: bug fix for testing project 110.258S, v0.2
    - 2023-12-28: test with eris piepline 1.5.0, v0.3
    - 2024-01-04: add cmd interface, v0.4
"""
__version__ = '0.4'
import os 
import tempfile
import textwrap
import inspect
import shutil
import re
import datetime
import logging
import getpass
import glob
import warnings
import subprocess

import numpy as np
import astropy.table as table
import astropy.units as units
from astropy.io import fits
from astropy.utils.exceptions import AstropyWarning
from astropy.wcs import WCS
from astropy.wcs import utils as wcs_utils
from astropy import stats as astro_stats
from astroquery.eso import Eso
from astroquery.eso.core import NoResultsWarning
import requests 

# for fits combination
from reproject import mosaicking
from reproject import reproject_adaptive, reproject_exact


#####################################
######### DATA Retrieval ############

def download_file(url, filename=None, outdir='./', auth=None): 
    """download files automatically 

    Features:
    1. fast
    2. redownload failed files
    3. skip downloaded files

    Args:
        url (str): the full url of the file
        filename (str): the filename to be saved locally
        outdir (str): the output directory
        auth (str): the authentication if needed
    """
    is_downloaded = False
    if not os.path.isdir(outdir):
        subprocess.run(['mkdir', '-p', outdir])
    with requests.get(url, auth=auth, stream=True) as r:
        if filename is None:
            # automatically define the filename
            try:
                filename_match = re.compile('filename=(?P<filename>[\w.\-\:]+)')
                filename = filename_match.search(r.headers['Content-Disposition']).groupdict()['filename']
            except:
                logging.warning(f"Failed to find the filename from headers, set to Undefined")
                filename = 'Undefined'
        filename_fullpath = os.path.join(outdir, filename)
        # check the local file if it exists
        if os.path.isfile(filename_fullpath):
            filesize = os.path.getsize(filename_fullpath)
            try:
                if str(filesize) == r.headers['Content-Length']:
                    logging.info(f"{filename} is already downloaded.")
                    is_downloaded = True
                else:
                    logging.warning('Find local inconsistent file, overwriting...')
            except:
                logging.warning(f'Overwriting {filename_fullpath}')
        if not is_downloaded:
            with open(filename_fullpath, 'wb') as f:
                shutil.copyfileobj(r.raw, f)

def read_metadata(metadata):
    """United way to read metadata
    """
    if isinstance(metadata, str):
        meta_tab = table.Table.read(metadata, format='csv')
    elif isinstance(metadata, table.Table):
        meta_tab = metadata
    else:
        print(metadata)
        raise ValueError(f'Unsupported file type of metadata: {type(metadata)}')
    try: meta_tab.sort(['Release Date'])
    except: pass
    return meta_tab

def save_metadata(metadata, metafile='metadata.csv'):
    """United way to save metadata
    """
    try:
        if len(metadata) > 0:
            if os.path.isfile(metafile):
                subprocess.run(['mv', metafile, metafile+'.bak'])
            else:
                if '/' in metafile:
                    subprocess.run(['mkdir', '-p', os.path.dirname(metafile)])
            metadata.sort(['Release Date'])
            metadata.write(metafile, format='csv')
    except:
        raise ValueError('Unsupported metadata!')

def download_eris(eris_query_tab, outdir='raw', metafile=None, username=None):
    """download the calib files of eris (wrapper of download_file)

    Args:
        eris_query_tab (astropy.table): the query table returned by astroquery.eso
        outdir (str): the directory to store the download files and saved meta table
        metafile (str): the filename of the saved tabe from eris_query_tab
        save_columns (list): the selected column names to be saved.
                             set the 'None' to save all the columns
    """
    root_calib_url = 'https://dataportal.eso.org/dataportal_new/file/'
    if username is not None:
        passwd = getpass.getpass(f'{username} enter your password:\n')
        auth = requests.auth.HTTPBasicAuth(username, passwd)
    else: auth = None
    for fileid in eris_query_tab['DP.ID']:
        file_url = root_calib_url+fileid
        download_file(file_url, outdir=outdir, auth=auth)
    save_metadata(eris_query_tab, metafile=metafile)

def eris_auto_quary(start_date, end_date=None, start_time=12, end_time=12, max_days=40, 
                    column_filters={}, dry_run=False, debug=False, **kwargs):
    """query ESO/ERIS raw data from the database

    Args:
        start_date (str): the starting date: lile 2023-04-08
        end_date (str): the same format as start_date, the default value is start_date + 1day
        column_filters: the parameters of the query form
                        such as: 
                        column_filters = {
                                'dp_cat': 'CALIB',
                                'dp_type': 'FLAT%',
                                'seq_arm': 'SPIFFIER',
                                'ins3_spgw_name': 'K_low',
                                'ins3_spxw_name': '100mas',}
        max_days: the maximum days to search for the availble calibration files
        **kwargs: the keyword arguments of the possible column filters
                  such as: dp_tech='IFU', see more options in: 
                  https://archive.eso.org/wdb/wdb/cas/eris/form
    """
    if column_filters is None:
        column_filters={}
    for key, value in kwargs.items():
        column_filters[key] = value
    eso = Eso()
    eso.ROW_LIMIT = -1 # remove the row limit of eso.query_instrument
    sdatetime = datetime.datetime.strptime(f'{start_date} {start_time:0>2d}', '%Y-%m-%d %H')
    if end_date is not None:
        edatetime = datetime.datetime.strptime(f'{end_date} {end_time:0>2d}', '%Y-%m-%d %H')
        sdatetime = (edatetime - sdatetime)/2 + sdatetime
    delta_time = datetime.timedelta(days=1)
    matched = 0
    for i in range(0, max_days):
        if matched == 0:
            t_start = (sdatetime - 0.5*datetime.timedelta(days=i))
            t_end = (sdatetime + 0.5*datetime.timedelta(days=i))
            column_filters['stime'] = t_start.strftime('%Y-%m-%d')
            column_filters['etime'] = t_end.strftime('%Y-%m-%d')
            column_filters['starttime'] = t_start.strftime('%H')
            column_filters['endtime'] = t_end.strftime('%H')
            warnings.simplefilter('ignore', category=NoResultsWarning)
            tab_eris = eso.query_instrument('eris', column_filters=column_filters)
            if tab_eris is not None:
                matched = 1
    if matched == 0:
        # print("Cannot find proper calib files, please check your input")
        logging.warning("eris_auto_quary: cannot find proper calib files!")
    else:
        if dry_run:
            print(column_filters)
        return tab_eris

def request_calib(start_date=None, band=None, spaxel=None, exptime=None, 
                  outdir='raw', end_date=None, dpcat='CALIB', arm='SPIFFIER', 
                  metafile=None, max_days=40,
                  steps=['dark','detlin','distortion','flat','wavecal'],
                  dry_run=False, debug=False, **kwargs):
    """a general purpose to qeury calib files of ERIS/SPIFFIER observation
    
    Args:
        start_date (str): ISO date format, like: 2023-04-08
        end_date (str, None): same format as start_date
        band (str): grating configurations
        spaxel (str): the spaxel size of the plate, [250mas, 100mas, 25mas]
        exptime (int,float): the exposure time, in seconds
        outdir (str): the output directory of the download files
        steps (list): a list of calibration steps, the connection with DRP types:
            
            --------------------------------------------------------------------------
            name         dp_type           function
            --------------------------------------------------------------------------
            dark         DARK              dark data reduction (daily)
            detlin       LINEARITY%        detector's non linear bad pixels (monthly)
            distortion   NS%               distortion correction
            flat         FLAT%             flat field data reduction
            wavecal      WAVE%             wavelength calibration
            stdstar      %STD%             std stars, including the flux stdstar
            psfstar      %PSF-CALIBRATOR   the psfstar
            --------------------------------------------------------------------------

        dpcat (str): default to be 'CALIB'
        arm (str): the instrument arm of eris: SPIFFIER or NIX (in develop)
        dry_run: return the list of files instead of downloading them

    """
    dptype_dict = {'dark':'DARK', 'detlin':'LINEARITY%', 'distortion':'NS%',
                   'flat':'FLAT%', 'wavecal':'WAVE%', 'stdstar':'%STD%', 
                   'psfstar': '%PSF-CALIBRATOR'}
    if exptime is None: exptime = ''
    query_tabs = []
    
    if debug:
        print("Input parameters:")
        print(f'steps: {steps}')
        print(f'spaxel: {spaxel}')
        print(f'band: {band}')
        print(f'exptime: {exptime}')
        print(f'max_days: {max_days}')
    for step in steps:
        logging.info(f'Requesting {step} calibration files')
        column_filters = {'dp_cat': dpcat,
                          'seq_arm': arm,
                          'dp_type': dptype_dict[step]}
        if step == 'dark':
            # drop the requirement for band and spaxel
            column_filters['exptime'] = exptime
        if step in ['distortion', 'flat', 'wavecal', 'stdstar', 'psfstar']:
            column_filters['ins3_spgw_name'] = band
            column_filters['ins3_spxw_name'] = spaxel

        step_query = eris_auto_quary(start_date, end_date=end_date, column_filters=column_filters,
                                     dry_run=dry_run, debug=debug, **kwargs)
        # fix the data type issue of masked table columns
        if step_query is not None:
            for col in step_query.colnames:
                step_query[col] = step_query[col].astype(str)
            query_tabs.append(step_query)
        else:
            # raise ValueError('Failed in requesting the calib files! Consider release the `max_days`?')
            logging.warning(f"request_calib: no calib file found for '{step}', consider to relax the `max_days`?'")
        if len(query_tabs) > 0:
            all_tabs = table.vstack(query_tabs)
        else:
            all_tabs = []
    if dry_run:
        return all_tabs
    else:
        if len(all_tabs) > 0:
            download_eris(all_tabs, metafile=metafile, outdir=outdir)
        else:
            logging.warning("No files for downloading!")

def request_science(prog_id='', username=None, metafile='metadata.csv',
                    outdir=None, target='', ob_id='', exptime='',
                    start_date='', end_date='', debug=False, **kwargs):
    """download the science data 

    To download the proprietory data, you need to provide your eso username
    and you will be asked to input your password.

    Args:
        prog_id (str): program id
        username (str): the user name of your eso account
        metafile (str): the output file to store all the meta data
                        default: metadata.csv
        target (str): the target name
        outdir (str): the directory to save all the raw files
        ob_id (str, int): the id of the observation
        start_date (str): starting data, in the format of '2023-04-08'
        end_date (str): end date, same format as start_date
        **kwargs: other keyword filters
    """
    root_calib_url = 'https://dataportal.eso.org/dataportal_new/file/'
    if outdir is None:
        if prog_id is not None: outdir = prog_id+'_raw'
        else: outdir = 'raw'
    if os.path.isdir(outdir):
        subprocess.run(['mkdir', '-p', outdir])
    logging.info(f'Requesting the data from project: {prog_id}')
    eso = Eso()
    eso.ROW_LIMIT = -1
    if end_date is None:
        sdate = datetime.datetime.strptime(start_date, '%Y-%m-%d')
        edate = sdate + datetime.timedelta(days=1)
        end_date = edate.strftime('%Y-%m-%d')
    eris_query_tab = eso.query_instrument(
            'eris', column_filters={'ob_id': ob_id,
                                    'prog_id':prog_id,
                                    'stime': start_date,
                                    'exptime': exptime,
                                    'etime': end_date,
                                    'target': target,})
    if debug:
        return eris_query_tab
    else:
        download_eris(eris_query_tab, username=username, outdir=outdir, metafile=metafile)

def generate_metadata(data_dir=None, header_dir=None, metafile='metadata.csv', 
                      extname='PRIMARY', work_dir=None, clean_work_dir=False,
                      dry_run=False, debug=False, overwrite=False):
    """generate metafile from download files

    Args:
        data_dir (str): the directory include the fits file
    """
    # colnames
    colnames = ['Release Date', 'Object', 'RA', 'DEC','Program ID', 'DP.ID', 'EXPTIME', 
                'OB.ID', 'OBS.TARG.NAME', 'DPR.CATG', 'DPR.TYPE', 'DPR.TECH', 'TPL.START', 
                'SEQ.ARM', 'DET.SEQ1.DIT', 'INS3.SPGW.NAME', 'INS3.SPXW.NAME','ARCFILE']
    colnames_header = ['DATE', 'OBJECT', 'RA', 'DEC', 'HIERARCH ESO OBS PROG ID', 
                       'ARCFILE', 'EXPTIME',
                       'HIERARCH ESO OBS ID', 'HIERARCH ESO OBS TARG NAME',  
                       'HIERARCH ESO DPR CATG', 'HIERARCH ESO DPR TYPE', 
                       'HIERARCH ESO DPR TECH', 'HIERARCH ESO TPL START', 
                       'HIERARCH ESO SEQ ARM', 'HIERARCH ESO DET SEQ1 DIT',
                       'HIERARCH ESO INS3 SPGW NAME', 'HIERARCH ESO INS3 SPXW NAME', 'ARCFILE']
    # check the exiting metafile
    if metafile is not None:
        if os.path.isfile(metafile):
            if not overwrite:
                if debug: print("Exiting file found, skip it...")
                return
        else:
            subprocess.run(['mkdir','-p', os.path.dirname(metafile)])
    if data_dir is not None:
        data_dir = data_dir.strip('/')
        fits_Zfiles = glob.glob(data_dir+'/*.fits.Z')
        fits_files = glob.glob(data_dir+'/*.fits')
        if work_dir is None:
            work_dir = '.tmp_generate_metadata'
            clean_work_dir = True

        dir_uncompressed = os.path.join(work_dir, 'uncompressed')
        dir_header = os.path.join(work_dir, 'headers')
        for d in [dir_uncompressed, dir_header]:
            if not os.path.isdir(d):
                subprocess.run(['mkdir', '-p', d])
        # compress the fits file
        if len(fits_Zfiles) > 0:
            for ff in fits_Zfiles:
                subprocess.run(['cp', ff, dir_uncompressed])
            # subprocess.run(['uncompress', dir_uncompressed+'/*.Z'])
            os.system(f'uncompress {dir_uncompressed}/*.Z')
            fits_Zfiles = glob.glob(f'{dir_uncompressed}/*.fits')
        
        fits_files = fits_files + fits_Zfiles
        # extract info from the headers and save the info
        meta_tab = table.Table(names=colnames, dtype=['U32']*len(colnames))
        if len(fits_files) > 0:
            for ff in fits_files:
                with fits.open(ff) as hdu:
                    header = hdu[extname].header
                    # [header[cn] if cn in header.keys() else '' for cn in colnames_header]
                    header_values = []
                    for cn in colnames_header:
                        try: header_values.append(str(header[cn]).removesuffix('.fits'))
                        except: header_values.append('')
                    meta_tab.add_row(header_values)
    elif header_dir is not None:
        fits_headers = glob.glob(header_dir+'/*.hdr')
        meta_tab = table.Table(names=colnames, dtype=['U32']*len(colnames))
        if len(fits_headers) > 0:
            for fh in fits_headers:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', AstropyWarning)
                    header = fits.header.Header.fromtextfile(fh)
                    header_values = []
                    for cn in colnames_header:
                        try: header_values.append(str(header[cn]).removesuffix('.fits'))
                        except: header_values.append('')
                    meta_tab.add_row(header_values)
    # clean working directory
    if clean_work_dir:
        subprocess.run(['rm', '-rf', work_dir])
    # save the header metadata
    if dry_run:
        print(f"Read {len(meta_tab)} files, no problem found.")
    if debug:
        print('The metadata table:')
        print(meta_tab)
    if metafile is not None:
        save_metadata(meta_tab, metafile=metafile)
        return metafile
    else:
        return meta_tab

#####################################
######### DATA Calibration ##########

def search_static_calib(esorex):
    # use the default staticPool
    if '/' in esorex:
        try:
            binpath_match = re.compile('(?P<bindir>^[\/\w\s\_\.\-]*)/esorex')
            bindir = binpath_match.search(esorex).groupdict()['bindir']
        except:
            raise ValueError('Failed to locate the install direction of esorex!')
    else: 
        bindir = os.path.dirname(shutil.which(esorex))
    install_dir = os.path.dirname(bindir)
    static_pool_list = glob.glob(os.path.join(install_dir, 'calib/eris-*'))
    static_pool = sorted(static_pool_list)[-1] # choose the latest one
    return static_pool

def generate_calib(metadata, raw_pool='./raw', work_dir=None, 
                   calib_pool='./calibPool', static_pool=None,
                   steps=['dark','detlin','distortion','flat','wavecal'],
                   dark_sof=None, detlin_sof=None, distortion_sof=None, flat_sof=None, 
                   wavecal_sof=None, drp_type_colname='DPR.TYPE', 
                   esorex=None, dry_run=False, archive=False, archive_name=None,
                   debug=False):
    """generate the science of frame of each calibration step

    Args:
        metafile (str): the metadata file where you can find the path and objects of each dataset
        raw_pool (str): the raw data pool
        static_pool (str): the static data pool
        calib_pool (str): the directory to keep all the output files from esorex
        steps (list): the steps to generate the corresponding calibration files
                      it could include: ['dark','detlin','distortion','flat','wavecal']
        drp_type_colname (str): the column name of the metadata table includes the DPR types. 
        esorex (str): the callable esorex command from the terminal
        dry_run (str): set it to True to just generate the sof files
        archive (bool): switch to archive mode, where the calibPool is organised in the folder
                        based structures, the subfolder name is specified by archive_name
        archive_name (str): the archive name. It is suggested to be the date of the observation
    """
    cwd = os.getcwd()
    calib_pool = calib_pool.rstrip('/')
    raw_pool = raw_pool.rstrip('/')
    if static_pool is None:
        # use the default staticPool
        static_pool = search_static_calib(esorex)
    else: static_pool = static_pool.rstrip('/')
    if archive:
        if archive_name is None:
            # use the date tag as the archive name
            # archive_name = datetime.date.today().strftime("%Y-%m-%d")
            raise ValueError('Please give the archive name!')
        calib_pool = os.path.join(calib_pool, archive_name)
        if not os.path.isdir(calib_pool):
            subprocess.run(['mkdir', '-p', calib_pool])
        sof_name = f'{archive_name}.sof'
        work_dir = calib_pool
    else:    
        sof_name = 'esorex_ifu_eris.sof'
    # setup directories
    if work_dir is None:
        work_dir = '.'
    for dire in [work_dir, calib_pool]:
        if not os.path.isdir(dire):
            subprocess.run(['mkdir', '-p', dire])

    meta_tab = read_metadata(metadata)
    try: meta_tab.sort(['Release Date']); 
    except: pass

    if 'dark' in steps:
        if dark_sof is None:
            # generate the sof for dark calibration
            dark_sof = os.path.join(work_dir, 'dark.sof')
            # dark_sof = os.path.join(calib_pool, sof_name)
            with open(dark_sof, 'w+') as openf:
                for item in meta_tab[meta_tab[drp_type_colname] == 'DARK']:
                    openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z DARK\n")
                # read the timestamp and exptime
                timestamp = item['Release Date']
                exptime = item['DET.SEQ1.DIT']
                openf.write(f'# dark: date={timestamp} exptime={exptime}\n')
        if not dry_run:
            subprocess.run([esorex, f'--output-dir={calib_pool}', 'eris_ifu_dark', dark_sof])
            # if rename
                # # rename the files with keywords
                # dark_bpm_fits = f'{calib_pool}/eris_ifu_dark_bpm_{exptime:0.1f}s_{timestamp}.fits'
                # dark_master_fits = f'{calib_pool}/eris_ifu_dark_master_{exptime:0.1f}s_{timestamp}.fits'
            # else:
                # dark_bpm_fits = f'{calib_pool}/eris_ifu_dark_bpm.fits'
                # dark_master_fits = f'{calib_pool}/eris_ifu_dark_master'
            # os.system(f'mv {work_dir}/eris_ifu_dark_bpm.fits {dark_bpm_fits}')
            # os.system(f'mv {work_dir}/eris_ifu_dark_bpm.fits {dark_master_fits}')
    if 'detlin' in steps:
        if detlin_sof is None:
            # generate the sof for detector's linarity
            detlin_sof = os.path.join(work_dir, 'detlin.sof')
            # detlin_sof = os.path.join(calib_pool, sof_name)
            with open(detlin_sof, 'w+') as openf:
                for item in meta_tab[meta_tab[drp_type_colname] == 'LINEARITY,DARK,DETCHAR']:
                    openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z LINEARITY_LAMP\n")
                for item in meta_tab[meta_tab[drp_type_colname] == 'LINEARITY,LAMP,DETCHAR']:
                    openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z LINEARITY_LAMP\n")
                # read the timestamp
                timestamp = item['Release Date']
                openf.write(f'# detlin: date={timestamp}\n')
        if dry_run:
            print(f"{esorex_cmd} eris_ifu_detlin {detlin_sof}")
        else:
            subprocess.run([esorex, f'--output-dir={calib_pool}', 'eris_ifu_detlin', detlin_sof])
            # if rename:
                # detlin_bpm_filt_fits = f'{calib_pool}/eris_ifu_detlin_bpm_filt_{timestamp}.fits'
                # detlin_bpm_fits = f'{calib_pool}/eris_ifu_detlin_bpm_{timestamp}.fits'
                # detlin_gain_info_fits = f'{calib_pool}/eris_ifu_detlin_gain_info_{timestamp}.fits'
            # else:
                # detlin_bpm_filt_fits = f'{calib_pool}/eris_ifu_detlin_bpm_filt.fits'
                # detlin_bpm_fits = f'{calib_pool}/eris_ifu_detlin_bpm.fits'
                # detlin_gain_info_fits = f'{calib_pool}/eris_ifu_detlin_gain_info.fits'
            # os.system(f'mv {work_dir}/eris_ifu_detlin_bpm_filt.fits {detlin_bpm_filt_fits}')
            # os.system(f'mv {work_dir}/eris_ifu_detlin_bpm.fits {detlin_bpm_fits}')
            # os.system(f'mv {work_dir}/eris_ifu_detlin_gain_info.fits {detlin_gain_info_fits}')

    if 'distortion' in steps:
        if distortion_sof is None:
            # generate the sof for distortion
            distortion_sof = os.path.join(work_dir, 'distortion.sof')
            # distortion_sof = os.path.join(calib_pool, sof_name)
            with open(distortion_sof, 'w+') as openf:
                for item in meta_tab[meta_tab[drp_type_colname] == 'NS,DARK']:
                    openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z DARK_NS\n")
                for item in meta_tab[meta_tab[drp_type_colname] == 'NS,SLIT']:
                    openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z FIBRE_NS\n")
                for item in meta_tab[meta_tab[drp_type_colname] == 'NS,WAVE,DARK']:
                    openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z WAVE_NS\n")
                for item in meta_tab[meta_tab[drp_type_colname] == 'NS,WAVE,LAMP']:
                    openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z WAVE_NS\n")
                for item in meta_tab[meta_tab[drp_type_colname] == 'NS,FLAT,DARK']:
                    openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z FLAT_NS\n")
                for item in meta_tab[meta_tab[drp_type_colname] == 'NS,FLAT,LAMP']:
                    openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z FLAT_NS\n")
                openf.write(f"{static_pool}/eris_ifu_first_fit.fits FIRST_WAVE_FIT\n") 
                openf.write(f"{static_pool}/eris_ifu_ref_lines.fits REF_LINE_ARC\n")
                openf.write(f"{static_pool}/eris_ifu_wave_setup.fits WAVE_SETUP\n")
                # read the timestamp, band, and spaxel
                timestamp = item['Release Date']
                band = item['INS3.SPGW.NAME']
                spaxel = item['INS3.SPXW.NAME']
                openf.write(f'# distortion: date={timestamp} band={band} spaxel={spaxel}\n')
        if not dry_run:
            subprocess.run([esorex, f'--output-dir={calib_pool}', 'eris_ifu_distortion', distortion_sof])
            # if rename:
                # distortion_bpm_fits = f'{calib_pool}/eris_ifu_distortion_bpm_{band}_{spaxel}_{timestamp}.fits'
                # distortion_distortion_fits = f'{calib_pool}/eris_ifu_distortion_distortion_{band}_{spaxel}_{timestamp}.fits'
                # distortion_slitlet_pos_fits = f'{calib_pool}/eris_ifu_distortion_slitlet_pos_{band}_{spaxel}_{timestamp}.fits'
            # else:
                # distortion_bpm_fits = f'{calib_pool}/eris_ifu_distortion_bpm.fits'
                # distortion_distortion_fits = f'{calib_pool}/eris_ifu_distortion_distortion.fits'
                # distortion_slitlet_pos_fits = f'{calib_pool}/eris_ifu_distortion_slitlet_pos.fits'
            # os.system(f'mv {work_dir}/eris_ifu_distortion_bpm.fits {distortion_bpm_fits}')
            # os.system(f'mv {work_dir}/eris_ifu_distortion_distortion.fits {distortion_distortion_fits}')
            # os.system(f'mv {work_dir}/eris_ifu_distortion_slitlet_pos.fits {distortion_slitlet_pos_fits}')
    if 'flat' in steps:
        if flat_sof is None:
            # generate the sof for flat
            flat_sof = os.path.join(work_dir, 'flat.sof')
            # flat_sof = os.path.join(calib_pool, sof_name)
            with open(flat_sof, 'w+') as openf:
                for item in meta_tab[meta_tab[drp_type_colname] == 'FLAT,DARK']:
                    openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z FLAT_LAMP\n")
                for item in meta_tab[meta_tab[drp_type_colname] == 'FLAT,LAMP']:
                    openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z FLAT_LAMP\n")
                openf.write(f"{calib_pool}/eris_ifu_dark_bpm.fits BPM_DARK\n")
                openf.write(f"{calib_pool}/eris_ifu_detlin_bpm_filt.fits BPM_DETLIN\n")
                openf.write(f"{calib_pool}/eris_ifu_distortion_bpm.fits BPM_DIST\n")
                # read the timestamp, band, and spaxel
                timestamp = item['Release Date']
                band = item['INS3.SPGW.NAME']
                spaxel = item['INS3.SPXW.NAME']
                openf.write(f'# flat: date={timestamp} band={band} spaxel={spaxel}\n')
        if not dry_run:
            subprocess.run([esorex, f'--output-dir={calib_pool}', 'eris_ifu_flat', flat_sof])
            # if rename:
                # flat_bpm_fits = f'{calib_pool}/eris_ifu_flat_bpm_{band}_{spaxel}_{timestamp}.fits'
                # flat_master_flat_fits = f'{calib_pool}/eris_ifu_flat_master_flat_{band}_{spaxel}_{timestamp}.fits'
            # else:
                # flat_bpm_fits = f'{calib_pool}/eris_ifu_flat_bpm.fits'
                # flat_master_flat_fits = f'{calib_pool}/eris_ifu_flat_master_flat.fits'
            # os.system(f'mv {work_dir}/eris_ifu_flat_bpm.fits {flat_bpm_fits}')
            # os.system(f'mv {work_dir}/eris_ifu_flat_bpm.fits {flat_master_flat_fits}')
    if 'wavecal' in steps:
        if wavecal_sof is None:
            # generate the sof for wavecal
            wavecal_sof = os.path.join(work_dir, 'wavecal.sof')
            # wavecal_sof = os.path.join(calib_pool, sof_name)
            with open(wavecal_sof, 'w+') as openf:
                for item in meta_tab[meta_tab[drp_type_colname] == 'WAVE,DARK']:
                    openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z WAVE_LAMP\n")
                for item in meta_tab[meta_tab[drp_type_colname] == 'WAVE,LAMP']:
                    openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z WAVE_LAMP\n")
                openf.write(f"{calib_pool}/eris_ifu_distortion_distortion.fits DISTORTION\n")
                openf.write(f"{static_pool}/eris_ifu_ref_lines.fits REF_LINE_ARC\n")
                openf.write(f"{static_pool}/eris_ifu_wave_setup.fits WAVE_SETUP\n") 
                openf.write(f"{static_pool}/eris_ifu_first_fit.fits FIRST_WAVE_FIT\n") 
                openf.write(f"{calib_pool}/eris_ifu_flat_master_flat.fits MASTER_FLAT\n")
                openf.write(f"{calib_pool}/eris_ifu_flat_bpm.fits BPM_FLAT\n")
                # read the timestamp, band, and spaxel
                timestamp = item['Release Date']
                band = item['INS3.SPGW.NAME']
                spaxel = item['INS3.SPXW.NAME']
                openf.write(f'# wavecal: date={timestamp} band={band} spaxel={spaxel}\n')
        if not dry_run:
            subprocess.run([esorex, f'--output-dir={calib_pool}', 'eris_ifu_wavecal', wavecal_sof])
            # if rename:
                # wave_map_fits = f'{calib_pool}/eris_ifu_wave_map_{band}_{spaxel}_{timestamp}.fits'
                # wave_arcImg_resampled_fits = f'{calib_pool}/eris_ifu_wave_arcImag_resampled_{band}_{spaxel}_{timestamp}.fits'
                # wave_arcImg_stacked_fits = f'{calib_pool}/eris_ifu_wave_arcImag_stacked_{band}_{spaxel}_{timestamp}.fits'
            # else:
                # wave_map_fits = f'{calib_pool}/eris_ifu_wave_map.fits'
                # wave_arcImg_resampled_fits = f'{calib_pool}/eris_ifu_wave_arcImag_resampled.fits'
                # wave_arcImg_stacked_fits = f'{calib_pool}/eris_ifu_wave_arcImag_stacked.fits'
            # os.system(f'mv {work_dir}/eris_ifu_wave_map.fits {wave_map_fits}')
            # os.system(f'mv {work_dir}/eris_ifu_wave_arcImag_resampled.fits {wave_arcImg_resampled_fits}')
            # os.system(f'mv {work_dir}/eris_ifu_wave_arcImag_stacked.fits {wave_arcImg_stacked_fits}')

def auto_jitter(metadata=None, raw_pool=None, outdir='./', calib_pool='calibPool', 
                sof=None,
                static_pool=None, esorex='', mode='jitter',
                objname=None, band=None, spaxel=None, exptime=None, 
                dpr_tech='IFU', dpr_catg='SCIENCE', prog_id=None, ob_id=None,
                dry_run=False, debug=False):
    """calibrate the science target or the standard stars
    """
    calib_pool = calib_pool.rstrip('/')

    if sof is not None:
        auto_jitter_sof = sof
    else:
        meta_tab = read_metadata(metadata)
        
        if not os.path.isdir(outdir):
            subprocess.run(['mkdir','-p',outdir])
        
        if static_pool is None:
            # use the default staticPool
            static_pool = search_static_calib(esorex)
        else:
            static_pool = static_pool.rstrip('/')
        

        # apply the selections
        if ob_id is not None:
            meta_tab = meta_tab[meta_tab['OB.ID'].astype(type(ob_id)) == ob_id]
        if objname is not None:
            meta_tab = meta_tab[meta_tab['Object'] == objname]
        if band is not None:
            meta_tab = meta_tab[meta_tab['INS3.SPGW.NAME'] == band]
        if spaxel is not None:
            meta_tab = meta_tab[meta_tab['INS3.SPXW.NAME'] == spaxel]
        if exptime is not None:
            meta_tab = meta_tab[(meta_tab['DET.SEQ1.DIT']-exptime)<1e-6]
        if prog_id is not None:
            meta_tab = meta_tab[meta_tab['Program ID'] == prog_id]
        if dpr_tech is not None:
            dpr_tech_select = [True if dpr_tech in item['DPR.TECH'] else False for item in meta_tab]
            meta_tab = meta_tab[dpr_tech_select]
        if dpr_catg is not None:
            meta_tab = meta_tab[meta_tab['DPR.CATG'] == dpr_catg]

        if len(meta_tab) < 1:
            print(" >> skipped, non-science data")
            logging.warning(f"skipped {objname}({ob_id}) with {band}+{spaxel}+{exptime}, non-science data")
            return
        auto_jitter_sof = os.path.join(outdir, 'auto_jitter.sof')
        with open(auto_jitter_sof, 'w+') as openf:
            # write OBJ
            if mode == 'jitter':
                n_obj = 0
                n_sky = 0
                for item in meta_tab[meta_tab['DPR.CATG'] == 'SCIENCE']:
                    if item['DPR.TYPE'] == 'OBJECT':
                        openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z OBJ\n")
                        n_obj += 1
                    elif item['DPR.TYPE'] == 'SKY':
                        openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z SKY_OBJ\n")
                        n_sky += 1
                if (n_sky < 1) or (n_obj < 1):
                    print(f" >> skipped, on find {n_obj} science frame and {n_sky} sky frame")
                    logging.warning(f"skipped {objname}({ob_id}) with {band}+{spaxel}+{exptime}, only find {n_obj} science frame and {n_sky} sky frame")
                    return

            elif mode == 'stdstar':
                stdstar_names = meta_tab[meta_tab[drp_type_colname] == 'STD']['OBS.TARG.NAME'].data.tolist()
                if len(stdstar_names)>1:
                    logging.warning(f'Finding more than one stdstar: {stdstar_names}') 
                    logging.warning(f'Choosing the first one: {stdstar_names[0]}')
                stdstar_name = stdstar_names[0]
                for item in meta_tab[meta_tab[drp_type_colname] == 'STD']:
                    if item['OBS.TARG.NAME'] == stdstar_name:
                        openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z STD #{stdstar_name}\n")
                    else: openf.write(f"#{raw_pool}/{item['DP.ID']}.fits.Z STD #{item['OBS.TARG.NAME']}\n")
                for item in meta_tab[meta_tab[drp_type_colname] == 'SKY,STD']:
                    if item['OBS.TARG.NAME'] == stdstar_name:
                        openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z SKY_STD #{stdstar_name}\n")
                    else: openf.write(f"#{raw_pool}/{item['DP.ID']}.fits.Z SKY_STD #{item['OBS.TARG.NAME']}\n")
                # for psf stars
                for item in meta_tab[meta_tab[drp_type_colname] == 'PSF,SKY,STD']: #TODO
                    if item['OBS.TARG.NAME'] == stdstar_name:
                        openf.write(f"{raw_pool}/{item['DP.ID']}.fits.Z PSF_CALIBRATOR #{stdstar_name}\n")
                    else: openf.write(f"#{raw_pool}/{item['DP.ID']}.fits.Z SKY_PSF_CALIBRATOR #{item['OBS.TARG.NAME']}\n")
            openf.write(f"{calib_pool}/eris_ifu_distortion_distortion.fits DISTORTION\n")
            openf.write(f"{calib_pool}/eris_ifu_wave_map.fits WAVE_MAP\n")
            #openf.write(f"{calib_pool}/eris_ifu_distortion_slitlet_pos.fits SLITLET_POS\n")
            openf.write(f"{calib_pool}/eris_ifu_flat_master_flat.fits MASTER_FLAT\n")
            openf.write(f"{calib_pool}/eris_ifu_dark_master_dark.fits MASTER_DARK\n")
            #openf.write(f"{static_pool}/EXTCOEFF_TABLE.fits EXTCOEFF_TABLE\n")
            openf.write(f"{static_pool}/eris_oh_spec.fits OH_SPEC\n")
            if band in ['H_low', 'J_low', 'K_low']:
                openf.write(f"{static_pool}/RESPONSE_WINDOWS_{band}.fits RESPONSE\n")

    if not dry_run:
        if mode == 'stdstar':
            subprocess.run([esorex, f'--output-dir={outdir}', 'eris_ifu_stdstar', 
                            auto_jitter_sof])
        elif mode == 'jitter':
            subprocess.run([esorex, f'--output-dir={outdir}', 
                            # run the pipeline even ecount corrupted or missing files
                            '--check-sof-exist=false', 
                            'eris_ifu_jitter', 
                            '--product_depth=2', '--sky_tweak=0', 
                            '--dar-corr=true', '--cube.combine=FALSE', auto_jitter_sof])

#####################################
######### DATA Combination ##########

def fix_micron_unit_header(header):
    """this small program fix the unrecongnized unit "micron" by astropy.wcs

    *only tested with the 3D datacube from VLT/ERIS
    """
    if 'CUNIT3' in header:
        if header['CUNIT3'] == 'MICRON':
            header['CUNIT3'] = 'um'
    return header

def find_combined_wcs(image_list=None, wcs_list=None, header_ext='DATA', frame=None, 
                      pixel_size=None, pixel_shifts=None):
    """compute the final coadded wcs

    It suports the combination of the 3D datacubes.
    It uses the first wcs to comput the coverage of all the images;
    Then, it shifts the reference point to the center.
    If spaxel provided, it will convert the wcs to the new spatial pixel size

    Args:
        image_list (list, tuple, np.ndarray): a list fitsfile, astropy.io.fits.header, 
                                              or astropy.wcs.WCS <TODO>
        wcs_list (list, tuple, np.ndarray): a list of astropy.wcs.WCS, need to include
                                            the shape information
        header_ext (str): the extension name of the fits card
        frame (astropy.coordinate.Frame): The sky frame, by default it will use the 
                                          frame of the first image
        pixel_size (float): in arcsec, the final pixel resolution of the combined image <TODO>
        pixel_shifts (list, tuple, np.ndarray): same length as image_list, with each 
                                                element includes the drift in each 
                                                dimension, in the order of [(drift_x(ra),
                                                drift_y(dec), drift_chan),]
    """
    # if the input is fits files, then first calculate their wcs
    if image_list is not None:
        wcs_list = []
        for i,fi in enumerate(image_list):
            with fits.open(fi) as hdu:
                header = fix_micron_unit_header(hdu[header_ext].header)
                image_wcs = WCS(hdu[header_ext].header)
                wcs_list.append(image_wcs)
    
    # check the shape of the shifts
    n_wcs = len(wcs_list)
    if pixel_shifts is not None:
        if len(pixel_shifts) != n_wcs:
            raise ValueError("Pixel_shift does not match the number of images or WCSs!")
        pixel_shifts = np.array(pixel_shifts)

    # get the wcs of the first image
    first_wcs = wcs_list[0] 
    first_shape = first_wcs.array_shape # [size_chan, size_y, size_x]
    naxis = first_wcs.wcs.naxis

    # then looping through all the images to get the skycoord of the corner pixels
    if naxis == 2: # need to reverse the order of the shape size
        # compute the two positions: [0, 0], [size_x, size_y]
        corner_pixel_coords = [(0,0), np.array(first_shape)[::-1]-1] # -1 because the index start at 0
    elif naxis == 3:
        # compute three positions: [0,0,0], [size_x, size_y, size_chan]
        corner_pixel_coords = [(0,0,0), np.array(first_shape)[::-1]-1]
    else: 
        raise ValueError("Unsupport datacube! Check the dimentions of the datasets!")
    image_wcs_list = []
    corners = []
    resolutions = []
    for i,fi in enumerate(wcs_list):
            image_wcs = wcs_list[i]
            if pixel_shifts is not None:
                image_wcs.wcs.crpix -= pixel_shifts[i]
            array_shape = image_wcs.array_shape
            # get the skycoord of corner pixels
            for pixel_coord in corner_pixel_coords:
                # pixel order: [x, y, chan]
                corner = wcs_utils.pixel_to_pixel(image_wcs, first_wcs, *pixel_coord)
                corners.append(corner)
            resolutions.append(wcs_utils.proj_plane_pixel_scales(image_wcs))

    # calculate the reference point
    corners = np.array(corners)
    low_boundaries = np.min(corners, axis=0)
    up_boundaries = np.max(corners, axis=0)
    ranges = np.round(up_boundaries - low_boundaries + 1).astype(int) # [range_x, range_y, range_chan]
    chan0 = low_boundaries[0]
    x0, y0 = ranges[:2]*0.5 # only need the first two for x and y

    # get the skycoord of the reference point
    reference_skycoord = wcs_utils.pixel_to_skycoord(x0, y0, wcs=first_wcs)

    # assign the new reference to the new wcs
    wcs_combined = first_wcs.deepcopy()
    if naxis == 3:
        # shift the reference point to the center
        # reference channel point the first channel of the combined data
        try: dchan = first_wcs.wcs.cd[-1,-1]
        except:
            try: dchan = first_wcs.wcs.pc[-1,-1]
            except:  
                raise ValueError("Cannot read the step size of the spectral dimension!")

        reference_chan = first_wcs.wcs.crval[-1] + (first_wcs.wcs.crpix[-1]-chan0-1)*dchan
        wcs_combined.wcs.crval = np.array([reference_skycoord.ra.to(units.deg).value, 
                         reference_skycoord.dec.to(units.deg).value,
                         reference_chan])
        wcs_combined.wcs.crpix = np.array([x0, y0, 1])
        # wcs_combined.wcs.cdelt = wcs.wcs.cd.diagonal() # cdelt will be ignored when CD is present
    elif naxis == 2:
        wcs_combined.wcs.crval = np.array([reference_skycoord.ra.to(units.deg).value, 
                                           reference_skycoord.dec.to(units.deg).value])
    wcs_combined.array_shape = tuple(ranges[::-1]) # need to reverse again

    # by default, the pixel size of the first image will be used
    # update the pixel size if needed
    # if pixel_size is not None: #<TODO>: untested
        # min_resolutions = np.min(np.array(resolutions), axis=1)
        # scales = min_resolutions / first_resolutions
        # wcs_new = wcs_combined.deepcopy()
        # wcs_new.wcs.cd = wcs_new.wcs.cd * scales
        # if (scales[-1] - 1) > 1e-6:
            # nchan = int(wcs_combined.array_shape[0] / scales)
        # x0_new, y0_new = wcs_utils.skycoord_to_pixel(reference_skycoord)
        # wcs_new.crpix = np.array([x0_new.item(), y0_new.item(), 1]).astype(int)
        # wcs_new.array_shape = tuple(np.round(np.array(wcs_combined.array_shape) / scales).astype(int))
        # wcs_combined = wcs_new
    return wcs_combined

def find_combined_wcs_test(image_list, wcs_list=None, header_ext='DATA', frame=None, 
                           pixel_size=None, ):
    """this is just a wrapper of reproject.mosaicking.find_optimal_celestial_wcs
    
    Used to test the performance of `find_combined_wcs`
    """
    # define the default values
    image_wcs_list = []
    for img in image_list:
        # read the image part
        with fits.open(img) as hdu:
            header = hdu[header_ext].header
            image_shape = (header['NAXIS2'], header['NAXIS1'])
            nchan = header['NAXIS3']
            image_wcs = WCS(header).celestial #sub(['longitude','latitude'])
            if frame is None:
                frame = wcs_utils.wcs_to_celestial_frame(image_wcs)    
            image_wcs_list.append((image_shape, image_wcs))
    wcs_combined, shape_combined = mosaicking.find_optimal_celestial_wcs(
            tuple(image_wcs_list), frame=frame, resolution=pixel_size)
    return wcs_combined, shape_combined

def compute_weighting_eris(image_list, mode='exptime', header_ext='DATA'):
    """compute the weighting of each image

    1. computing the weighting based on the integration time
    2. based on the RMS level
    """
    if mode == 'exptime':
        total_time = 0
        time_list = []
        for img in image_list:
            with fits.open(img) as hdu:
                header = hdu[header_ext].header
                total_time += header['EXPTIME']
                time_list.append(header['EXPTIME'])
        return np.array(time_list)/total_time

def fill_mask(image, mask):
    """Using iterative median to filled the masked region
    In each cycle, the masked pixel is set to the median value of all the values in the 
    surrounding region (in cubic 3x3 region, total 8 pixels)
    Inspired by van Dokkum+2023 (PASP) and extended to support 3D datacube
    
    Args:
        image (ndarray): the input image
        mask (ndarray): the same shape as image, with masked pixels are 1 and rest are 0
    """
    ndim = image.ndim
    image_filled = image.copy().astype(float)
    image_filled[mask==1] = np.nan
    image_shape = np.array(image.shape)
    up_boundaries = np.repeat(image_shape, 2) - 1
    mask_idx = np.argwhere(mask > 0)
    while np.any(np.isnan(image_filled)):
        for idx in mask_idx:
            idx_range = np.array([[i-1,i+2] for i in idx])
            # check if reaches low boundaries, 0
            if np.any(idx < 1):  
                idx_range[idx_range < 0] = 0
            # check if reach the upper boundaries
            if np.any(image_shape - idx < 1):
                idx_range[idx_range>up_boundaries] = up_boundaries[idx_range>up_boundaries]
            ss = tuple(np.s_[idx_range[i][0]:idx_range[i][1]] for i in range(ndim))
            image_filled[tuple(idx)] = np.nanmedian(image_filled[ss])
    return image_filled

def construct_wcs(header, data_shape=None):
    """try to construct the wcs from a broken header 

    TODO: not tested
    """
    return
    try:
        # read some useful information
        ndim = header['NAXIS']
        crpix1, crpix2 = header['CRPIX1'], header['CRPIX2']
        ra, dec = header['CRVAL1'], header['CRVAL2']
        cdelt1, cdelt2 = header['CD1_1'], header['CD2_2']
        cunit1, cunit2 = header['CUNIT1'], header['CUNIT2']
        if ndim>2:
            crpix3 = header['CRPIX3']
            crvar3 = header['CRVAL3']
            cdelt3 = header['CD3_3']
            cunit3 = header['CUNIT3']
    except:
        ra, dec = header['RA'], heaer['DEC']
        crpix1, crpix2 = xsize/2., ysize/2.
        cdelt1, cdelt2 = spaxel/3600., spaxel/3600.
        cunit1, cunit2 = 'deg', 'deg'
        if ndim>2:
            # should be fine for given random units, as all the wcs 
            # shares the same units
            crpix3 = 1
            crvar3 = 1
            cdelt3 = 1 
            cunit1 = 'um'
    if True:
        data_shape = hdu[data_ext].data.shape
        ndim = len(data_shape)
        ysize, xsize = data_shape[-2:]
        print('Warning: making use of mock wcs!')
        wcs_mock = WCS(naxis=ndim)
        if ndim == 2:
            wcs_mock.wcs.crpix = crpix1, crpix2
            wcs_mock.wcs.cdelt = cdelt1, cdelt2
            wcs_mock.wcs.cunit = 'deg', 'deg'
            wcs_mock.wcs.ctype = 'RA', 'DEC'
            wcs_mock.array_shape = [ysize, xsize]
        elif ndim == 3:
            wcs_mock.wcs.crpix = crpix1, crpix2, crpix3
            wcs_mock.wcs.cdelt = cdelt1, cdelt2, cdelt3
            wcs_mock.wcs.cunit = 'deg', 'deg', 'um'
            wcs_mock.wcs.ctype = 'RA', 'DEC', 'Wavelength'
            wcs_mock.array_shape = [ndim, ysize, xsize]

def data_combine(image_list, data_ext='DATA', mask=None, mask_ext='DQI',  
                 pixel_shifts=None, ignore_wcs=False, 
                 sigma_clip=True, sigma=3.0, bgsub=True,
                 header_ext=None, weighting=None, frame=None, projection='TAN', 
                 pixel_size=None, savefile=None):
    """combine the multiple observation of the same target

    By default, the combined wcs uses the frame of the first image

    sigma_clip, apply if there are large number of frames to be combined
    background: global or chennel-per-channel and row-by-row
    Args:
        bgsub (bool): set to true to subtract global thermal background
        sigma_clip (bool): set to true to apply sigma_clip with the sigma controlled
                           by `sigma`
        sigma (bool): the deviation scale used to control the sigma_clip
    """
    # define the default variables
    # if isinstance(image_list, 'str'):
        # if os.path.isfile(image_list):
            # image_list = np.loadtxt(image_list)
    nimages = len(image_list)
    if header_ext is None:
        header_ext = data_ext

    # check the input variables
    if pixel_shifts is not None:
        if len(pixel_shifts) != nimages:
            raise ValueError("Pixel_shift does not match the number of images!")
        pixel_shifts = np.array(pixel_shifts)

    if ignore_wcs:
        # ignore the wcs and mainly relies on the pixel_shifts to align the images
        # to make advantage of reproject, we still need a roughly correct wcs or 
        # a mock wcs
        # first try to extract basic information from the first image
        with fits.open(image_list[0]) as hdu:
            header = fix_micron_unit_header(hdu[header_ext].header)
            try:
                # if the header have a rougly correct wcs
                wcs_mock = WCS(header)
            except:
                wcs_mock = construct_wcs(header, data_shape=None)
        # shifting the mock wcs to generate a series of wcs
        wcs_list = []
        if pixel_shifts is not None:
            for i in range(nimages):
                wcs_tmp = wcs_mock.deepcopy()
                x_shift, y_shift = pixel_shifts[i]
                wcs_tmp.wcs.crpix += np.array([x_shift, y_shift, 0])
                wcs_list.append(wcs_tmp)
        else:
            wcs_list = [wcs_mock]*nimages
    else:
        wcs_list = []
        # looping through the image list to extract their wcs
        for i,fi in enumerate(image_list):
            with fits.open(fi) as hdu:
                # this is to fix the VLT micron header
                header = fix_micron_unit_header(hdu[header_ext].header)
                image_wcs = WCS(header)
                wcs_list.append(image_wcs)
    # compute the combined wcs 
    wcs_combined = find_combined_wcs(wcs_list=wcs_list, frame=frame, 
                                     pixel_size=pixel_size)
    shape_combined = wcs_combined.array_shape
    if len(shape_combined) == 3:
        nchan, size_y, size_x = shape_combined
    elif len(shape_combined) == 2:
        size_y, size_x = shape_combined

    # define the combined cube
    image_shape_combined = shape_combined[-2:]
    data_combined = np.full(shape_combined, fill_value=0.)
    coverage_combined = np.full(shape_combined, fill_value=1e-8)
    
    # handle the weighting
    if weighting is None:
        # treat each dataset equally
        weighting = np.full(nimages, fill_value=1./nimages)
    
    # reproject each image to the combined wcs
    for i in range(nimages):
        image_wcs = wcs_list[i].celestial
        data = fits.getdata(image_list[i], data_ext)
        
        if mask_ext is not None:
            mask = fits.getdata(image_list[i], mask_ext)
        else:
            mask = np.full(data.shape, fill_value=False)

        # check if the channel length consistent with the combined wcs
        if data.ndim == 3: #<TODO>: find a better way to do
            if len(data) != shape_combined[0]:
                logging.warning("Combining data with different channels!")
            if len(data) >= shape_combined[0]:
                data = data[:shape_combined[0]]
                mask = mask[:shape_combined[0]]
            else:
                data_shape = data.shape
                data_shape_extend = [shape_combined[0], data_shape[1], data_shape[2]]
                data_extend = np.full(data_shape_extend, fill_value=0.)
                mask_extend = np.full(data_shape_extend, fill_value=False)
                data_extend[:data_shape[0]] = data[:]
                mask_extend[:data_shape[0]] = mask[:]
                data = data_extend
                mask = mask_extend

        # reset the masked value to zero, to be removed from combination
        # <TODO>: find a better way to fix the masked pixels
        # now we just set it to zeros
        data_masked = np.ma.masked_array(data, mask=mask)

        if sigma_clip:
            data_masked = astro_stats.sigma_clip(
                    data_masked, sigma=sigma, maxiters=5, masked=True)
        if bgsub:
            # subtract a median background in each channel
            data_masked = data_masked - np.ma.median(
                            data_masked, axis=(1,2))[:, np.newaxis, np.newaxis]
        data = data_masked.filled(0)
        mask = data_masked.mask
        # 
        data_reprojected, footprint = reproject_adaptive((data, image_wcs), 
                                                          wcs_combined.celestial, 
                                                          shape_out=shape_combined,
                                                          conserve_flux=True)
        mask_reprojected, footprint = reproject_adaptive((mask, image_wcs), 
                                                          wcs_combined.celestial, 
                                                          shape_out=shape_combined,
                                                          conserve_flux=False)
        data_combined += data_reprojected * weighting[i]
        footprint = footprint.astype(bool)
        coverage_combined += (1.-mask_reprojected)
        # error2_combined += error_reprojected**2 * weighting[i]
    # error_combined = np.sqrt(error2_combined)
    data_combined = data_combined / coverage_combined

    if savefile is not None:
        # save the combined data
        hdr = wcs_combined.to_header() 
        hdr['OBSERVER'] = 'MPE-IR'
        hdr['COMMENT'] = 'Combined by eris_jhchen_utils.py'
        # reset the cdelt
        if 'CD1_1' in header.keys():
            header['CDELT1'] = header['CD1_1']
            header['CDELT2'] = header['CD2_2']
            header['CDELT3'] = header['CD3_3']
        elif 'PC1_1' in header.keys():
            header['CDELT1'] = header['PC1_1']
            header['CDELT2'] = header['PC2_2']
            header['CDELT3'] = header['PC3_3']
        primary_hdu = fits.PrimaryHDU(header=hdr)
        data_combined_hdu = fits.ImageHDU(data_combined, name="DATA", header=hdr)
        hdus = fits.HDUList([primary_hdu, data_combined_hdu])
        hdus.writeto(savefile, overwrite=True)
    else:
        return data_combined, error_combined

def data_combine_pixel(image_list, offsets=None, savefile=None):
    """deprecated, will be removed in the future

    this function combine the image/datacube in the pixel space

    image_list (list): the list of filenames or ndarray
    offset (list,ndarray): the offset (x, y) of each image
    """
    data_combined = 0.0
    coverage_combined = 1.0
    
    # quick test without offsets
    if offsets is None:
        for i,image in enumerate(image_list):
            header = fits.getheader(image, 'DATA')
            image_data = fits.getdata(image, 'DATA')
            image_mask = fits.getdata(image, 'DQI')
            image_data[(1.0-image_mask)<1e-6] = 0.0
            if i == 0:
                data_combined = np.zeros_like(image_data)
                coverage_combined = np.zeros_like(image_mask)
                header_combined = fits.getheader(image, 0)
            data_combined += image_data
            coverage_combined += (1 - image_mask)
    else:
        # calculate the combined image size
        # to make things simpler, here we still keep the x and y equal
        padding = np.max(offsets)
        for i,image in enumerate(image_list):
            header = fits.getheader(image, 'DATA')
            image_data = fits.getdata(image, 'DATA')
            image_mask = fits.getdata(image, 'DQI')
            if i==0:
                # skip the resampling
                ygrid, xgrid = np.mgrid(ny, nx) + padding
                pass
            else:
                # get the real pixel coordinate of the image
                ygrid, xgrid = np.mgrid(ny, nx) + padding + offsets[i]
                # get the nearest grid pixel
                ygrid2, xgrid2 = np.round(ygrid), np.round(xgrid)
                # resample the image to be aligned with the grid
                image_resampling(image, offset)
                scipy.interpolate.griddata(np.array(list(zip(ygrid, xgrid))), 
                                           image_data.ravel(), 
                                           np.array(list(zip(ygrid2, xgrid2))),
                                           method='linear',)

    data_combined = data_combined / coverage_combined

    if savefile is not None:
        # save the combined data
        hdr = header_combined 
        hdr['OBSERVER'] = ''
        hdr['COMMENT'] = ''
        primary_hdu = fits.PrimaryHDU(header=hdr)
        data_combined_hdu = fits.ImageHDU(data_combined, name="DATA", header=hdr)
        # error_combined_hdu = fits.ImageHDU(error_combined, name="ERROR", header=hdr)
        hdus = fits.HDUList([primary_hdu, data_combined_hdu])
        hdus.writeto(savefile, overwrite=True)
    else:
        return data_combined 

def read_eris_drifts(datfile, arcfilenames):
    """read eris drifting table
    """
    pixel_center = [32., 32.] # the expected center
    dat = table.Table.read(datfile, format='csv')
    drifts = np.zeros((len(arcfilenames), 2)) # drift in [x, y]
    for i in range(len(arcfilenames)):
        arcfile = arcfilenames[i]
        dat_img = dat[dat['ARCFILE'] == arcfile]
        if len(dat_img) == 1:
            drifts[i] = [dat_img['x_model'][0]-pixel_center[0], dat_img['y_model'][0]-pixel_center[1]]
        else:
            print("Drifts not found!")
            drifts[i] = [0.,0.]
    return drifts

def compute_eris_offset(image_list, additional_drifts=None, header_ext='Primary',
                        header_ext_data='DATA',
                        ra_offset_header='HIERARCH ESO OCS CUMOFFS RA',
                        dec_offset_header='HIERARCH ESO OCS CUMOFFS DEC',
                        x_drift_colname='x_model', y_drift_colname='y_model',
                        coord_system='sky'):
    """compute the eris offset based on the telescope pointing

    This program will read the cumulative OCS offset from the header of each image,
    then it will compute the relative offset compare to the first image.
    The OCS offset is the expected offset from the telescope, but it may not always
    perform so accurately. If there is additional dirfts, it can also be accounted
    
    Args:
        image_list: the fits images, with the header include the offset information
        additional_drifts: (str, ndarray): the additional drift in pixels
    """
    # initialise the reference point
    nimage = len(image_list)
    array_offset = np.zeros((nimage, 2))
    arcfilenames = []
    # the followind code assuming the relative offset is small, so the sky offset has
    # been directly converted into pixel offset
    for i, img in enumerate(image_list):
        with fits.open(img) as hdu:
            header = hdu[header_ext].header
            data_header = hdu[header_ext_data].header
            arcfilenames.append(header['ARCFILE'])
            ra_offset = header[ra_offset_header]
            dec_offset = header[dec_offset_header]
            ra_diff = abs(data_header['CD1_1']*3600.)
            dec_diff = abs(data_header['CD2_2']*3600.)
            if i == 0: 
                ra_offset_0 = ra_offset
                dec_offset_0 = dec_offset
                # convert the skycoords to pixels use the first wcs
                if coord_system == 'sky':
                    image_wcs = WCS(header)
            array_offset[i][:] = (ra_offset-ra_offset_0)/ra_diff, (dec_offset-dec_offset_0)/dec_diff
    # consider additional offset
    print(">>>>>>>>\nOCS offset:")
    print(array_offset)
    if additional_drifts is not None:
        if isinstance(additional_drifts, str):
            additional_drifts = read_eris_drifts(additional_drifts, arcfilenames)
            print('++++++++\n additional difts:')
            print(additional_drifts)
        for i in range(nimage):
            array_offset[i] += additional_drifts[i]
    return array_offset

def search_eris_files(dirname, pattern=''):
    matched_files = []
    # This will return absolute paths
    file_list = [f for f in glob.iglob(dirname.strip('/')+"/**", recursive=True) if os.path.isfile(f)]
    for ff in file_list:
        if re.search(pattern, os.path.basename(ff)):
            matched_files.append(ff)
    return matched_files

def combine_eris_ifu(image_list=None, dirname=None, pattern='', drifts_file=None, outfile=None, **kwargs):
    if dirname is not None:
        image_list = search_eris_files(dirname, pattern)
    weighting = compute_weighting_eris(image_list)
    drifts = compute_eris_offset(image_list, additional_drifts=drifts_file)
    data_combine(image_list, weighting=weighting, pixel_shifts=drifts, savefile=outfile, **kwargs)

#####################################
########### Quick Tools #############

def get_daily_calib(date, outdir, band, spaxel, exptime, esorex='esorex', 
                    overwrite=False):
    """A wrapper to get daily calibration file quickly
    """
    archive_name = f'{date}_{band}_{spaxel}_{exptime}s'
    archive_dir = os.path.join(outdir, archive_name)
    if os.path.isfile(archive_dir+'/eris_ifu_wave_map.fits'):
        if not overwrite:
            print(f"> re-use existing calibPool in {archive_dir}")
            return archive_dir
    with tempfile.TemporaryDirectory() as tmpdir:
        metafile = os.path.join(tmpdir, f'{date}_{band}_{spaxel}_{exptime}.csv')
        request_calib(start_date=date, band=band, spaxel=spaxel, exptime=exptime, 
                      outdir=tmpdir, metafile=metafile)
        archive_name = f'{date}_{band}_{spaxel}_{exptime}s'
        generate_calib(metafile, raw_pool=tmpdir, calib_pool=outdir, archive=True, 
                       archive_name=archive_name, esorex=esorex)
    return archive_dir

def run_eris_pipeline(datadir=None, outdir=None, esorex='esorex', overwrite=False, 
                      debug=False, dry_run=False):
    """A quick pipeline for archived eris data

    To run this pipeline, the input datadir is organised by the dates of the 
    observations. Within each date, all the relevant science data have been
    download. Within each folder, a subfold "headers" can be provide to speed
    up the analysis to identify their filetypes
    """
    # match all the dates
    datadir = datadir.strip('/')
    outdir = outdir.strip('/')
    date_matcher = re.compile(r'(\d{4}-\d{2}-\d{2})')
    date_list = []
    for subfolder in os.listdir(datadir):
        if date_matcher.match(subfolder):
            date_list.append(subfolder)

    # generate all the summary files
    for date in date_list:

        ## Step-1
        print(f"::eris_jhchen_utils:: generateing the metadata from {datadir}/{date}")
        logging.info(f"::eris_jhchen_utils:: generateing the metadata from {datadir}/{date}")
        # metadata = generate_metadata(os.path.join(datadir, date))
        date_metafile = os.path.join(outdir,date, f'{date}_metadata.csv')
        if os.path.isfile(date_metafile):
            # TODO: check the modified date of the files and metafile
            print(f"> finding existing metadata:{date_metafile}")
            logging.info(f"> finding existing metadata:{date_metafile}")
        else:
            print(f"> generating the metadata of {date}")
            logging.info(f"> generating the metadata of {date}")
            date_folder = os.path.join(datadir, date)
            # if os.path.isdir(date_folder + '/headers'):
                # metadata = generate_metadata(header_dir=date_folder+'/headers', 
                                             # metafile=date_metafile)
            # else:
                # with tempfile.TemporaryDirectory() as tmpdir:
                    # metadata = generate_metadata(data_dir=date_folder, work_dir=tmpdir, 
                                                 # metafile=date_metafile)

            with tempfile.TemporaryDirectory() as tmpdir:
                metadata = generate_metadata(data_dir=date_folder, work_dir=tmpdir, 
                                             metafile=date_metafile)

        ## Step-2
        print(f"::eris_jhchen_utils:: reducing the science data in {datadir}/{date}")
        logging.info(f"::eris_jhchen_utils:: reducing the science data in {datadir}/{date}")
    
        metadata = read_metadata(os.path.join(outdir, date, f'{date}_metadata.csv'))

        # filter out the non-science data
        # this will be done within auto_gitter, but here we can avoid generating 
        # the useless calibPool
        metadata = metadata[metadata['DPR.CATG'] == 'SCIENCE']
        dpr_tech_select = [True if 'IFU' in item['DPR.TECH'] else False for item in metadata]
        metadata = metadata[dpr_tech_select]
        if len(metadata) < 1:
            print(f"> No ERIS/SPIFFIER data found on {date}")
            logging.info(f"> No ERIS/SPIFFIER data found on {date}")

        # identify all the science objects and their OBs
        targets = np.unique(metadata['Object'])
        daily_calib_pool = os.path.join(outdir, date, 'calibPool')
        daily_datadir = os.path.join(datadir, date)
        for target in targets:
            target_metadata = metadata[metadata['Object']==target]
            ob_ids = np.unique(target_metadata['OB.ID'])
            for ob_id in ob_ids:
                # get the band, spaxel, exptime
                first_meta = target_metadata[target_metadata['OB.ID']==ob_id][0]
                band  = first_meta['INS3.SPGW.NAME']
                spaxel = first_meta['INS3.SPXW.NAME']
                exptime = int(first_meta['DET.SEQ1.DIT'])
                
                ## Step-3
                # generate the daily calibPool
                print(f"> generating calibPool for {date} with {band}+{spaxel}+{exptime}s")
                logging.info(f"> generating calibPool for {date} with {band}+{spaxel}+{exptime}s")
                try:
                    daily_id_calib_pool = get_daily_calib(date, daily_calib_pool, band, 
                                                          spaxel, exptime, esorex=esorex, 
                                                          overwrite=overwrite)
                except:
                    print(f"> Error found in geting the calibPool of {date}: {target}(OB.ID={ob_id}) with {band}+{spaxel}+{exptime}s")
                    logging.warning(f"> Error found in geting the calibPool of {date}: {target}(OB.ID={ob_id}) with {band}+{spaxel}+{exptime}s")

                ## Step-4
                # run eris_ifu_gitter
                daily_ob_outdir = os.path.join(outdir, date, 
                                         f'{target}_{ob_id}_{band}_{spaxel}_{exptime}s')
                if (os.path.isfile(os.path.join(daily_ob_outdir, 
                                              'eris_ifu_jitter_dar_cube_coadd.fits')) or 
                    os.path.isfile(os.path.join(daily_ob_outdir,
                                              'eris_ifu_jitter_obj_cube_coadd.fits'))):
                    if not overwrite:
                        print(f"> Done: {date}:{target}(OB.ID={ob_id}) with {band}+{spaxel}+{exptime}s")
                        logging.info(f"> Done: {date}:{target}(OB.ID={ob_id}) with {band}+{spaxel}+{exptime}s")
                        continue
                print(f"> working on {date}: {target}(OB.ID={ob_id}) with {band}+{spaxel}+{exptime}s")
                logging.info(f"> working on {date}: {target}(OB.ID={ob_id}) with {band}+{spaxel}+{exptime}s")
                try:
                    auto_jitter(metadata=target_metadata, raw_pool=daily_datadir, 
                                outdir=daily_ob_outdir, calib_pool=daily_id_calib_pool, 
                                ob_id=ob_id, esorex=esorex, mode='jitter', 
                                dry_run=dry_run)
                except:
                    subprocess.run(['rm','-rf', daily_ob_outdir])
                    print(f"> Error found in runing {date}: {target}(OB.ID={ob_id}) with {band}+{spaxel}+{exptime}s")
                    logging.warning(f"> Error found in runing {date}: {target}(OB.ID={ob_id}) with {band}+{spaxel}+{exptime}s")
                print("> Done!")

def quick_combine(datadir=None, target=None, offsets=None, excludes=None, band=None,
                  spaxel=None, drifts=None, outdir='./', esorex='esorex', 
                  savefile=None, suffix='combined', overwrite=False):
    """A wrapper of data_combine

    This quick tool search the all the available and valid observations
    and combine them with the data_combine.

    This tool take the outdir from `run_eris_pipeline` as input, it will search 
    all the available observations, and combined all the available data
    """
    target_matcher = re.compile("(?P<target>[\w\s\-\.+]+)_(?P<id>\d{7})_(?P<band>[JKH]_[\w]{3,6}?)_(?P<spaxel>\d{2,3}mas)_(?P<exptime>\d+)s")
    date_matcher = re.compile(r'(\d{4}-\d{2}-\d{2})')

    dates = os.listdir(datadir)
    image_list = []
    image_exp_list = []
    for date in dates:
        if not date_matcher.match(date):
            continue

        for obs in os.listdir(os.path.join(datadir, date)):
            try:
                obs_match = target_matcher.search(obs).groupdict()
            except:
                obs_match = None
                continue
            if obs_match is not None:
                obs_dir = os.path.join(datadir, date, obs)
                ob_target, ob_id= obs_match['target'], obs_match['id']
                ob_band, ob_spaxel = obs_match['band'], obs_match['spaxel']
                ob_exptime = obs_match['exptime']
            if ob_target != target:
                continue
            if (ob_band==band) and (ob_spaxel==spaxel):
                # combine the exposures within each OB
                exp_list = glob.glob(obs_dir+'/eris_ifu_jitter_dar_cube_[0-9]*.fits')
                # check the arcfile name not in the excludes file list
                exp_list_valid = []
                exp_list_arcfilenames = []
                for fi in exp_list:
                    with fits.open(fi) as hdu:
                        arcfile = hdu['PRIMARY'].header['ARCFILE']
                        if excludes is not None:
                            if arcfile in excludes:
                                continue
                        exp_list_valid.append(fi)
                        exp_list_arcfilenames.append(arcfile)

                if len(exp_list_valid) < 1:
                    print("no valid data for {target}({ob_id}) with {ob_band},{ob_spaxel},{ob_total_exptime}s")
                    continue
                # within each ob, the exposure time are equal, so we just ignore the
                # weighting
                ob_total_exptime = int(ob_exptime) * len(exp_list)
                obs_combined_filename = os.path.join(obs_dir, 
                    f"{target}_{ob_id}_{ob_band}_{ob_spaxel}_{ob_total_exptime}s_{suffix}.fits")

                if (not os.path.isfile(obs_combined_filename)) or overwrite:
                    obs_offset = compute_eris_offset(exp_list, additional_drifts=drifts)
                    data_combine(image_list=exp_list, pixel_shifts=obs_offset, 
                                 ignore_wcs=True, 
                                 savefile=obs_combined_filename)

                image_list.append(obs_combined_filename)
                image_exp_list.append(ob_total_exptime)
    # then, combine the data from different OB
    
    if len(image_list) < 1:
        print(f"no valid data for {target} with {band},{spaxel}")
        return
    # <TODO>: how to align different OBs
    total_exp = np.sum(image_exp_list)
    weighting = np.array(image_exp_list) / total_exp 
    print("combining images from:")
    with open(os.path.join(outdir,
              f'{target}_{band}_{spaxel}_{total_exp/3600:.1f}h_{suffix}_list.txt'), 'w+') as fp:
        n_combine = len(image_list)
        for i in range(n_combine):
            im = image_list[i]
            im_exptime = image_exp_list[i]
            print(f"  {im}")
            fp.write(f"{im} {im_exptime}\n")
            # with fits.open(im) as hdu:
                # print(hdu.info())
    if savefile is None:
        savefile = os.path.join(outdir, 
                    f'{target}_{band}_{spaxel}_{total_exp/3600:.1f}h_{suffix}.fits')
    data_combine(image_list=image_list, weighting=weighting, ignore_wcs=True,
                 mask_ext=None, savefile=savefile)


def eris_pipeline(project, start_date, band, spaxel, prog_id, 
                  username=None, end_date=None, ob_id='',
                  target='',
                  outdir='./', static_pool=None, esorex='esorex',
                  **kwargs):
    """simple pipeline for eris data reduction
    
    Args:
        project (str): the project code, used to orgnise the folder
        outdir (str): the output director. By default, a project folder
                      will be created inside the output directory
        
    """
    project_dir = os.path.join(outdir, project)
    if os.path.isdir(os.path.isdir(project_dir)):
        logging.warning("project folder existing! Reusing all the possible data!")
    
    # preparing the all the necessary folders
    project_calib_raw = project_dir+'/calib_raw'
    project_science_raw = project_dir+'/science_raw'
    project_calib_pool = project_dir+'/calibPool'
    project_calibrated = project_dir+'/calibrated'
    working_dirs = [project_calib_raw, project_science_raw, project_calib_pool, 
                    project_calibrated]
    for wd in working_dirs:
        if not os.path.isdir(wd):
            subprocess.run(['mkdir','-p', wd])
   
    # download the calibration and science raw files
    request_calib(start_date, band, spaxel, exptime, outdir=project_calib_raw,
                       end_date=end_date, **kwargs)
    request_science(prog_id=prog_id, username=username, 
                    outdir=project_science_raw,
                    start_date=start_date, end_date=end_date, 
                    ob_id=ob_id, target=target,
                    **kwargs)

    # generate the calib files
    generate_calib(metafile=os.path.join(raw_pool, 'metadata.csv'), 
                   raw_pool=project_calib_raw, static_pool=static_pool, 
                   calib_pool=project_calib_pool, drp_type_colname='DPR.TYPE',
                   esorex=esorex)
    
    # run calibration
    auto_jitter(metafile=os.path.join(raw_pool, 'metadata.csv'),
                raw_pool=project_science_raw, calib_pool=project_calib_pool,
                outdir=project_calibrated, static_pool=static_pool,
                grating=grating, esorex=esorex)


#####################################
######## helper functions ###########

def start_logger():
    logging.basicConfig(filename='myapp.log', level=logging.INFO)
    logger = logging.getLogger('simple_example')
    logger.setLevel(logging.INFO)
    logging.info('Started')
    pass
    logging.info('Finished')

#####################################
########## CMD wrapprt ##############
import argparse

if __name__ == '__main__':

    parser = argparse.ArgumentParser(
            usage='%(prog)s [options]',
            prog='eris_jhchen_utils.py',
            description="Welcome to jhchen's ERIS utilities",
            epilog='Reports bugs and problems to jhchen@mpe.mpg.de')
    parser.add_argument('--esorex', type=str, default='esorex',
                        help='specify the customed esorex')
    parser.add_argument('--debug', action='store_true',
                        help='dry run and print out all the input parameters')
    parser.add_argument('--dry_run', action='store_true',
                        help='print the commands but does not execute them')
    parser.add_argument('-v','--version', action='version', version=f'v{__version__}')

    # add subparsers
    subparsers = parser.add_subparsers(title='Available task', dest='task', 
                                       metavar=textwrap.dedent(
        '''
          * request_calib: search and download the raw calibration files
          * request_science: download the science data
          * generate_metadata: generate metadata from downloaded data
          * generate_calib: generate the calibration files
          * auto_jitter: run jitter recipe automatically
          * data_combine: combine the reduced data

          Quick tools:

          * get_daily_calib: quick way to get dalily calibration files
          * run_eris_pipeline: quick to reduce science data with raw files

          To get more details about each task:
          $ eris_jhchen_utils.py task_name --help
        '''))

    ################################################
    # request_calib
    subp_request_calib = subparsers.add_parser('request_calib',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=textwrap.dedent('''\
            Search and download the required calib files
            --------------------------------------------
            Examples:

              # request all the calibration data
              eris_jhchen_utils request_calib --start_date 2023-04-09 --band K_low --spaxel 100mas --exptime 600 --outdir ./raw --metafile raw/2023-04-09.metadata.csv

              # requst the calibration data for dark and detlin
              eris_jhchen_utils request_calib --steps dark detlin --start_date 2023-04-09 --band K_low --spaxel 100mas --exptime 600 --outdir ./raw --metafile raw/2023-04-09.metadata.csv

            '''))
    subp_request_calib.add_argument('--start_date', type=str, help='The starting date of the observation, e.g. 2023-03-08')
    subp_request_calib.add_argument('--end_date', type=str, help='The finishing date of the observation, e.g. 2023-03-08')
    subp_request_calib.add_argument('--steps', type=str, nargs='+', 
        help="Calibration steps, can be combination of: 'dark','detlin','distortion','flat','wavecal'",
                                     default=['dark','detlin','distortion','flat','wavecal'])
    subp_request_calib.add_argument('--band', type=str, help='Observing band')
    subp_request_calib.add_argument('--exptime', type=int, help='Exposure time')
    subp_request_calib.add_argument('--spaxel', type=str, help='Spatia pixel size')
    subp_request_calib.add_argument('--outdir', type=str, help='Output directory',
                                    default='raw')
    subp_request_calib.add_argument('--metafile', type=str, help='Summary file')
    subp_request_calib.add_argument('--max_days', type=int, help='Maximum searching days before and after the observing day.', default=40)
    subp_request_calib.add_argument('--debug', action='store_true',
                        help='dry run and print out all the input parameters')
    subp_request_calib.add_argument('--dry_run', action='store_true',
                        help='print the commands but does not execute them')
    
    
    ################################################
    # request_science
    subp_request_science = subparsers.add_parser('request_science',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=textwrap.dedent('''\
            search and download the science data
            ------------------------------------
            example:
            
            eris_jhchen_utils request_science --user username --prog_id 111.255U.002 --outdir science_raw --metafile science_raw/sience_metadata.csv

                                        '''))
    subp_request_science.add_argument('--start_date', type=str, help='The starting date of the observation. Such as 2023-03-08', default='')
    subp_request_science.add_argument('--band', type=str, help='Observing band', default='')
    subp_request_science.add_argument('--spaxel', type=str, help='Spatial pixel resolution', default='')
    subp_request_science.add_argument('--exptime', type=str, help='Integration time', default='')
    subp_request_science.add_argument('--username', type=str, help='The user name in ESO User Eortal.', default='')
    subp_request_science.add_argument('--outdir', type=str, help='Output directory')
    subp_request_science.add_argument('--prog_id', type=str, help='Program ID', default='')
    subp_request_science.add_argument('--ob_id', type=str, help='Observation ID', default='')
    subp_request_science.add_argument('--metafile', type=str, help='Summary file',default='metadata.csv')
    subp_request_science.add_argument('--end_date', type=str, help='The finishing date of the observation. Such as 2023-03-08', default='')
   

    ################################################
    # generate_metadata
    subp_generate_metadata = subparsers.add_parser('generate_metadata',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=textwrap.dedent('''\
            generate the metadata file from downloaded data
            -----------------------------------------------
            example:

                eris_jhchen_utils generate_metadata --header_dir science/2023-12-06/headers --metafile metadata/2023-12-06/metadata.csv
                
                eris_jhchen_utils generate_metadata --data_dir science/2023-12-06 --extname DATA --metafile metadata/2023-12-06/metadata.csv
                                        '''))

    subp_generate_metadata.add_argument('--data_dir', type=str, help='The directory with all the downloaded files, including all the *.fits.Z or *.fits')
    subp_generate_metadata.add_argument('--extname', type=str, help='The extension or card name of the targeted data in fits file', default='Primary')
    subp_generate_metadata.add_argument('--header_dir', type=str, help='The directory with all the processed headers, header files end with *.hdr')
    subp_generate_metadata.add_argument('--metafile', type=str, help='The output file with all the extracted informations from the fits headers')
    subp_generate_metadata.add_argument('--overwrite', action='store_true', help='Overwrite exiting metafile if present')

    
    ################################################
    # generate_calib
    subp_generate_calib = subparsers.add_parser('generate_calib',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=textwrap.dedent('''\
            generate the required calibration files
            ---------------------------------------
            example:
            
            esorex=~/esorex/bin/esorex
            
            # generate all the calibration files
            eris_jhchen_utils generate_calib --metadata raw/2023-04-09.metadata.csv --raw_pool raw --calib_pool calibPool --archive --archive_name 2023-04-09_Klow_100mas --esorex $esorex
            
            # only the specified step, eg: dark + detlin
            eris_jhchen_utils generate_calib --metadata raw/2023-04-09.metadata.csv --raw_pool raw --calib_pool calibPool --steps dark detlin --archive --archive_name 2023-04-09_Klow_100mas --esorex $esorex

                                        '''))
    subp_generate_calib.add_argument('--metadata', type=str, help='The summary file')
    subp_generate_calib.add_argument('--raw_pool', type=str, help='The directory includes the raw files')
    subp_generate_calib.add_argument('--calib_pool', type=str, help='The output directory',
                                     default='./calibPool')
    subp_generate_calib.add_argument('--esorex', nargs='?', type=str, default='esorex',
                                     help='specify the customed esorex')
    subp_generate_calib.add_argument('--static_pool', type=str, help='The static pool')
    subp_generate_calib.add_argument('--steps', type=str, nargs='+', 
        help="Calibration steps, can be combination of: 'dark','detlin','distortion','flat','wavecal'",
                                     default=['dark','detlin','distortion','flat','wavecal'])
    subp_generate_calib.add_argument('--dark_sof', help='dark sof')
    subp_generate_calib.add_argument('--detlin_sof', help='detector linearity sof')
    subp_generate_calib.add_argument('--distortion_sof', help='distortion sof')
    subp_generate_calib.add_argument('--flat_sof', help='flat sof')
    subp_generate_calib.add_argument('--wavecal_sof', help='wavecal sof')
    subp_generate_calib.add_argument('--archive', action='store_true', help='Turn on archive mode')
    subp_generate_calib.add_argument('--archive_name', help='Archive name')


    ################################################
    # auto_jitter
    subp_auto_jitter = subparsers.add_parser('auto_jitter',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=textwrap.dedent('''\
            automatically run the jitter recipe
            -----------------------------------
            Examples:

            eris_jhchen_utils auto_jitter --metadata science_raw/metadata.csv --raw_pool science_raw --calib_pool calibPool/2023-04-09_K_low_100mas_600s --ob_id 3589012 --outdir science_output 

            eris_jhchen_utils auto_jitter --sof manual.sof --raw_pool science_raw --calib_pool calibPool/2023-04-09_K_low_100mas_600s --outdir science_output

                                        '''))
    subp_auto_jitter.add_argument('--metadata', help='The summary file')
    subp_auto_jitter.add_argument('--sof', help='Specify the exisint sof')
    subp_auto_jitter.add_argument('--raw_pool', help='The folder name with all the raw files')
    subp_auto_jitter.add_argument('--outdir', help='The output directory')
    subp_auto_jitter.add_argument('--calib_pool', help='The folder with all the calibration files')
    subp_auto_jitter.add_argument('--static_pool', help='The folder with all the static calibration files')
    subp_auto_jitter.add_argument('--mode', help='The mode of the recipe, can be jitter and stdstar', default='jitter')
    subp_auto_jitter.add_argument('--objname', help='Select only the data with Object=objname')
    subp_auto_jitter.add_argument('--band', help='Select only the data with INS3.SPGW.NAME=band')
    subp_auto_jitter.add_argument('--spaxel', help='Select only the data with INS3.SPXW.NAME=spaxel')
    subp_auto_jitter.add_argument('--exptime', help='Select only the data with DET.SEQ1.DIT=exptime')
    subp_auto_jitter.add_argument('--dpr_tech', help='Select only the data with DPR.TECH=dpr_tech', default='IFU')
    subp_auto_jitter.add_argument('--prog_id', help='Select only the data with Program ID=prog_id')
    subp_auto_jitter.add_argument('--ob_id', help='Select only the data with OB.ID=prog_id')

    ################################################
    # combine data
    subp_data_combine = subparsers.add_parser('data_combine',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=textwrap.dedent('''\
            Combine reduced datacubes
            -------------------------
            Examples:

              eris_jhchen_utils run_eris_pipeline -d science_raw -o science_output -c calibPool
                                        '''))


    ################################################
    # get_daily_calib
    subp_get_daily_calib = subparsers.add_parser('get_daily_calib',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=textwrap.dedent('''\
            quickly get the daily calibration files
            ---------------------------------------
            example:
            
              eris_jhchen_utils get_daily_calib -d 2023-04-09 -o calibPool -b K_low -s 100mas -e 600
                                        '''))
    subp_get_daily_calib.add_argument('-d','--date', help='Observing date')
    subp_get_daily_calib.add_argument('-o','--outdir', help='Calibration Pool')
    subp_get_daily_calib.add_argument('-b','--band', help='Observation band')
    subp_get_daily_calib.add_argument('-s','--spaxel', help='Pixel size')
    subp_get_daily_calib.add_argument('-e','--exptime', help='Exposure time')
    subp_get_daily_calib.add_argument('--overwrite', action='store_true', 
                                      help='Overwrite the existing files')


    ################################################
    # run_eris_pipeline
    subp_run_eris_pipeline = subparsers.add_parser('run_eris_pipeline',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=textwrap.dedent('''\
            quickly reduce the science data
            -------------------------------
            example:

              eris_jhchen_utils run_eris_pipeline -d science_raw -o science_reduced -c calibPool
                                        '''))
    subp_run_eris_pipeline.add_argument('-d', '--datadir', 
                                          help='The folder with downloaded science data')
    subp_run_eris_pipeline.add_argument('-o', '--outdir', help='The output folder')
    subp_run_eris_pipeline.add_argument('--overwrite', action='store_true', 
                                      help='Overwrite the existing files')

    
    ################################################
    # quick combine
    subp_quick_combine = subparsers.add_parser('quick_combine',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=textwrap.dedent('''\
            quickly reduce the science data
            -------------------------------
            example:

              eris_jhchen_utils quick_combine --datadir science_output --target bx482 --band K_middle --spaxel 25mas --drifts drifts_file --suffix test1
                                        '''))
    subp_quick_combine.add_argument('--datadir')
    subp_quick_combine.add_argument('--target')
    subp_quick_combine.add_argument('--offsets')
    subp_quick_combine.add_argument('--excludes')
    subp_quick_combine.add_argument('--band')
    subp_quick_combine.add_argument('--spaxel')
    subp_quick_combine.add_argument('--drifts')
    subp_quick_combine.add_argument('--suffix', default='combined')

    ################################################
    # match the task name and pick the corresponding function
    args = parser.parse_args()
    ret = None # return status
    if args.debug:
        print(args)
        func_args = list(inspect.signature(locals()[args.task]).parameters.keys())
        func_str = f"Executing:\n \t{args.task}("
        for ag in func_args:
            try: func_str += f"{ag}={args.__dict__[ag]},"
            except: func_str += f"{ag}=None, "
        func_str += ')\n'
        print(func_str)
        print(f"Using esorex from {args.esorex}")
        print(f"Using static files from {search_static_calib(args.esorex)}")
    if args.task == 'request_calib':
        request_calib(start_date=args.start_date, band=args.band, steps=args.steps,
                      end_date=args.end_date, outdir=args.outdir, exptime=args.exptime, 
                      spaxel=args.spaxel, metafile=args.metafile, 
                      max_days=args.max_days, dry_run=args.dry_run, debug=args.debug)
    elif args.task == 'request_science':
        ret = request_science(prog_id=args.prog_id, 
                              ob_id=args.ob_id,
                              start_date=args.start_date, username=args.username,
                              band=args.band, spaxel=args.spaxel, 
                              exptime=args.exptime, end_date=args.end_date, 
                              outdir=args.outdir, metafile=args.metafile,
                              dry_run=args.dry_run, debug=args.debug)
    elif args.task == 'generate_metadata':
        generate_metadata(data_dir=args.data_dir, extname=args.extname,
                          header_dir=args.header_dir, metafile=args.metafile,
                          dry_run=args.dry_run, debug=args.debug, 
                          overwrite=args.overwrite)
    elif args.task == 'generate_calib':
        generate_calib(args.metadata, raw_pool=args.raw_pool, 
                       calib_pool=args.calib_pool, static_pool=args.static_pool, 
                       steps=args.steps, dark_sof=args.dark_sof, 
                       detlin_sof=args.detlin_sof, distortion_sof=args.distortion_sof,
                       flat_sof=args.flat_sof, wavecal_sof=args.wavecal_sof, 
                       archive=args.archive, archive_name=args.archive_name,
                       esorex=args.esorex, dry_run=args.dry_run, debug=args.debug)
    elif args.task == 'auto_jitter':
        ret = auto_jitter(metadata=args.metadata, raw_pool=args.raw_pool, 
                          sof=args.sof,
                          outdir=args.outdir, calib_pool=args.calib_pool, 
                          mode=args.mode, objname=args.objname, band=args.band, 
                          spaxel=args.spaxel, exptime=args.exptime, 
                          dpr_tech=args.dpr_tech, esorex=args.esorex, 
                          prog_id=args.prog_id, ob_id=args.ob_id, 
                          dry_run=args.dry_run, debug=args.debug)
    # the quick tools
    elif args.task == 'get_daily_calib':
        get_daily_calib(args.date, args.outdir, args.band, args.spaxel, args.exptime, 
                        esorex=args.esorex, overwrite=args.overwrite, debug=args.debug,
                        dry_run=args.dry_run)
    elif args.task == 'run_eris_pipeline':
        run_eris_pipeline(args.datadir, args.outdir, esorex=args.esorex, 
                          overwrite=args.overwrite, debug=args.debug,
                          dry_run=args.dry_run)
    elif args.task == 'quick_combine':
        quick_combine(datadir=args.datadir, target=args.target, offsets=args.offsets,
                      excludes=args.excludes, band=args.band, spaxel=args.spaxel,
                      drifts=args.drifts, suffix=args.suffix)
    else:
        pass
    if args.debug:
        if ret is not None:
            print(ret)
