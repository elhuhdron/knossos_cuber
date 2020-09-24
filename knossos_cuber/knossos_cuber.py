#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""A Python script that converts images into a **Knossos**-readable format."

   This script has been heavily modified from the original version.
"""

from functools import reduce

__author__ = 'Joergen Kornfeld'

import re
try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
import io
import math
import scipy.ndimage
import numpy as np
import multiprocessing as mp
from multiprocessing import managers
from PIL import Image
import os
import itertools
import scipy.special
import time
try:
    import fadvise
    FADVISE_AVAILABLE = True
except ImportError:
    FADVISE_AVAILABLE = False

import sys
from ast import literal_eval
from collections import OrderedDict, namedtuple
try:
    from ConfigParser import SafeConfigParser as ConfigParser
except ImportError:
    from configparser import ConfigParser
import argparse
import shutil
import glob

import h5py

#import skimage.transform
#from skimage import measure

import psutil
def get_mem_usage():
    return psutil.Process(os.getpid()).memory_info().rss // 2**20

#import libtiff
#import tifffile

# https://github.com/zimeon/iiif/issues/11
Image.MAX_IMAGE_PIXELS = 1e10

SOURCE_FORMAT_FILES = OrderedDict()
SOURCE_FORMAT_FILES['tif'] = ['tif', 'tiff', 'TIF', 'TIFF', '*.tif, *.tiff']
SOURCE_FORMAT_FILES['jpg'] = ['jpg', 'jpeg', 'JPG', 'JPEG', '*.jpg, *.jpeg']
SOURCE_FORMAT_FILES['png'] = ['png', 'PNG', '*.png']
SOURCE_FORMAT_FILES['hdf5'] = ['h5', 'H5', 'hdf5', 'HDF5', '*.h5', '*.hdf5']


class InvalidCubingConfigError(Exception):
    pass


class DownsampleJobInfo(object):
    def __init__(self):
        self.config = None
        self.trg_mag = 2
        self.from_raw = True
        self.src_cube_paths = []
        self.src_cube_local_coords = []
        self.trg_cube_path = ''
        self.trg_cube_path2 = ''
        self.cube_edge_len = 128
        self.skip_already_cubed_layers = False


class CompressionJobInfo(object):
    def __init__(self):
        self.src_cube_path = ''
        self.compressor = ''
        self.quality_or_ratio = 0
        self.pre_gauss = 0.0
        self.open_jpeg_bin_path = ''
        self.cube_edge_len = 128


def big_img_info(fn, dataset='image'):
    fh = h5py.File(fn, 'r')
    image = fh[dataset]
    img_shape = image.shape; img_dtype = image.dtype
    fh.close()
    return img_shape, img_dtype

def big_img_load(fn, dataset='image', rng=None):
    fh = h5py.File(fn, 'r')
    image = fh[dataset]
    if rng is None: rng = [[0,image.shape[0]], [0,image.shape[1]]]
    shape = [x[1] - x[0] for x in rng]
    img_blk = np.empty(shape, dtype=image.dtype)
    image.read_direct(img_blk, source_sel=np.s_[rng[0][0]:rng[0][1],rng[1][0]:rng[1][1]])
    fh.close()
    return img_blk

def get_list_of_all_cubes_in_dataset(dataset_base_path, log_fn):
    """TODO

    Args:
        dataset_base_path (str): Where `knossos_cuber()' stores the
                                 images.
        log_fn (function): A function that prints text.
    """
    all_cubes = []

    zero_dir = os.path.join(dataset_base_path, "x0000", "y0000", "z0000");
    allow_zerdir = False
    use_zerodir = False
    if os.path.exists(zero_dir) and allow_zerdir:
        log_fn("used zero dir for dimensions: {0} s".format(zero_dir))
        use_zerodir = True
        found_cube_files = os.listdir(zero_dir)
    else:
        log_fn("traversing dataset for dimensions")
        found_cube_files = []
        ref_time = time.time()
        for root, _, files in os.walk(dataset_base_path):
            cur_file = None
            for file in [f for f in files if os.path.basename(f).endswith(".jpg")]:
                cur_file = file
            for file in [f for f in files if os.path.basename(f).endswith(".raw")]:
                cur_file = file
            for file in [f for f in files if os.path.basename(f).endswith(".png")]:
                cur_file = file
            if cur_file is not None:
                all_cubes.append(os.path.join(root, cur_file))
            # if len(all_cubes) > 100:
            #     break
        found_cube_files.append(all_cubes[0])
        log_fn("Cube listing took: {0} s".format(time.time()-ref_time))

    print(found_cube_files[0])
    experimentname = re.compile(r"(?P<experimentname>.*)_mag.*\.(?P<extension>\w)").\
        search(os.path.basename(found_cube_files[0])).group("experimentname")
    log_fn("extracted experiment name: »{0}«".format(experimentname))

    from_raw = None
    for cur_file in found_cube_files:
        extension = os.path.splitext(cur_file)[1].lower()
        if extension == ".raw":
            from_raw = os.stat(os.path.join(dataset_base_path, "x0000", "y0000", "z0000", cur_file)).st_size
            break
        if extension != ".jpg" and extension != ".jpeg":
            break

    log_fn("using {0}".format(extension))

    if use_zerodir:
        mag = int(re.compile(r'.*mag(?P<magID>\d+)').search(dataset_base_path).group('magID'))

        x_count = len([name for name in os.listdir(dataset_base_path) \
            if os.path.isdir(os.path.join(dataset_base_path, name))])
        y_count = len([name for name in os.listdir(os.path.join(dataset_base_path, "x0000")) \
            if os.path.isdir(os.path.join(dataset_base_path, "x0000", name))])
        z_count = len([name for name in os.listdir(os.path.join(dataset_base_path, "x0000", "y0000")) \
            if os.path.isdir(os.path.join(dataset_base_path, "x0000", "y0000", name))])

        real_base = dataset_base_path.split("/mag")[0]
        print(real_base)
        for x in range(0, x_count):
            for y in range(0, y_count):
                for z in range(0, z_count):
                    all_cubes.append(get_cube_fname(real_base, experimentname, mag, x, y, z, extension)) # todo

        print("{0} {1} {2}: {3}".format(x_count, y_count, z_count, len(all_cubes)))
    else:
        print("{0} cubes".format(len(all_cubes)))

    return from_raw, all_cubes


def write_knossos_conf(data_set_base_folder='',
                       scale=(10., 10., 25.),
                       boundary=(1000, 1000, 1000),
                       exp_name='stack',
                       mag=1,
                       cube_edge_len=128):
    """Writes a knossos.conf file for the use with KNOSSOS."""

    if not os.path.exists(data_set_base_folder):
        os.makedirs(data_set_base_folder)

    with open(data_set_base_folder + '{0}.k.conf'.format(exp_name), 'w') as conf_file:
        conf_file.write("experiment name \"{0}\";\n".format(exp_name))
        conf_file.write("boundary x {0};\n".format(boundary[0]))
        conf_file.write("boundary y {0};\n".format(boundary[1]))
        conf_file.write("boundary z {0};\n".format(boundary[2]))
        conf_file.write("scale x {0};\n".format(scale[0]))
        conf_file.write("scale y {0};\n".format(scale[1]))
        conf_file.write("scale z {0};\n".format(scale[2]))
        conf_file.write("magnification {0};\n".format(mag))
        conf_file.write("cube_edge_length {0};\n".format(cube_edge_len))

    return


def get_cube_fname(basepath, expname, mag, x, y, z, extension):
    return '{basepath}/mag{mag}/x{x:04d}/y{y:04d}/z{z:04d}/{expname}_mag{mag}_x{x:04d}_y{y:04d}_z{z:04d}{ext}'\
        .format(
                **{'basepath': basepath,
                   'x': x,
                   'y': y,
                   'z': z,
                   'expname': expname,
                   'mag': mag,
                   'ext': extension})

def find_mag_folders(dataset_base_path, log_fn):
    mag_matcher = re.compile(r'.*mag(?P<magID>\d+)')
    found_mags = {}
    for subdir in [name for name in os.listdir(dataset_base_path)
               if os.path.isdir(os.path.join(dataset_base_path, name))]:
        mobj = mag_matcher.search(subdir)
        try:
            found_mags[int(mobj.group('magID'))] = subdir
        except:
            log_fn("Subdirectory found in the base folder that "
                   "does not comply with the KNOSSOS dataset standard: {0}"
                   .format(subdir))
    return found_mags

def downsample_dataset(config, src_mag, trg_mag, log_fn):
    dataset_base_path = config.get('Project', 'target_path')

    num_workers = config.getint('Processing', 'num_downsampling_cores')
    buffer_size_in_cubes_downsampling = \
        config.getint('Processing', 'buffer_size_in_cubes_downsampling')
    num_write_workers = config.getint('Processing', 'num_write_workers')

    mag_matcher = re.compile(r'.*mag(?P<magID>\d+)')
    found_mags = find_mag_folders(dataset_base_path, log_fn)
    for subdir in [name for name in os.listdir(dataset_base_path)
               if os.path.isdir(os.path.join(dataset_base_path, name))]:
        mobj = mag_matcher.search(subdir)
        try:
            found_mags[int(mobj.group('magID'))] = subdir
        except:
            log_fn("Subdirectory found in the base folder that "
                   "does not comply with the KNOSSOS dataset standard: {0}"
                   .format(subdir))

    # check if src mag is available
    if not src_mag in found_mags.keys():
        raise Exception("The src mag folder could not be found in the base "
                        "path folder.")

    log_fn("Analysing source dataset... (mag{0})".format(src_mag))

    # we walk through the dataset structure and collect all available cubes
    from_raw, all_cubes = get_list_of_all_cubes_in_dataset(
        dataset_base_path + '/' + found_mags[src_mag], log_fn)

    cube_edge_len = config.getint('Processing', 'cube_edge_len')

    source_dtype = None
    if 'source_dtype' in config['Dataset']:
        #if config.get('Dataset', 'source_dtype') == '':
        source_dtype = np.uint8 # todo
    elif from_raw is not None:
        if from_raw / cube_edge_len ** 3 == 2:
            log_fn("from 16 bit raw")
            source_dtype = np.uint16
        else:
            log_fn("from 8 bit raw")
            source_dtype = np.uint8

    # -> extract x,y,z src dataset dimensions in cubes
    cube_coord_matcher = re.compile(r'.*x(?P<x>\d+)_y(?P<y>\d+)_z('
                                    r'?P<z>\d+)\.(jpg|png|raw)$')

    max_x = 0
    max_y = 0
    max_z = 0

    path_hash = {}


    for this_cube_path in all_cubes:
        mobj = cube_coord_matcher.search(this_cube_path)

        try:
            x = int(mobj.group('x'))
            y = int(mobj.group('y'))
            z = int(mobj.group('z'))

        except:
            raise Exception("Error: Corrupt cube filename in list: {0}"
                            .format(this_cube_path))

        path_hash[(x, y, z)] = this_cube_path

        if x > max_x:
            max_x = int(mobj.group('x'))
        if y > max_y:
            max_y = int(mobj.group('y'))
        if z > max_z:
            max_z = int(mobj.group('z'))

    if max_x < 1 and max_y < 1 and max_z < 1:
        # nothing to downsample, stopping
        log_fn("Further downsampling is useless, stopping.")
        return False

    zanisotrop = trg_mag <= config.getint('Processing', 'keep_z_until_mag', fallback=1)

    out_path = dataset_base_path + '/mag' + str(trg_mag) + '/'
    log_fn("Downsampling to {0}, anisotropic z {1}".format(out_path, zanisotrop))

    log_fn("Src dataset cube dimensions: x {0}, y {1}, z {2}"
           .format(max_x+1, max_y+1, max_z+1))

    # create dummy K conf for mag detection
    magpath = dataset_base_path + '/mag' + str(trg_mag) + '/';
    if not os.path.exists(magpath):
        os.makedirs(magpath)
    fh=open(magpath + '/knossos.conf', 'w'); fh.close()

    # compile the 8 cubes that belong together, no overlap, set to 'bogus' at
    # the incomplete borders

    job_prep_time = time.time()
    log_fn("Preparing jobs…")

    experimentname = re.compile(r"(?P<experimentname>.*)_mag.*\.(?P<extension>\w)").\
        search(os.path.basename(all_cubes[0])).group("experimentname")

    downsampling_job_info = []
    skipped_count = 0
    skip_already_cubed_downsample_layers = config.getboolean('Processing', 'skip_already_cubed_downsample_layers')
    write_queue = mp.Queue(num_write_workers)
    for cur_x, cur_y, cur_z in itertools.product(range(0, max_x+2, 2),
                                                 range(0, max_y+2, 2),
                                                 range(0, max_z+2, 2)):

        if cur_x > max_x or cur_y > max_y or cur_z > max_z:
            path_hash[(cur_x, cur_y, cur_z)] = 'bogus'

        these_cubes = []
        these_cubes_local_coords = []
        for lx, ly, lz in itertools.product([0, 1], [0, 1], [0, 1]):

            # fill up the borders with black
            pos = (cur_x + lx, cur_y + ly, cur_z + lz)
            if pos not in path_hash:
                path_hash[pos] = 'bogus'

            these_cubes.append(path_hash[pos])
            these_cubes_local_coords.append((lx, ly, lz))

        if all(elem == 'bogus' for elem in these_cubes):
            continue

        this_job_info = DownsampleJobInfo()
        this_job_info.trg_mag = trg_mag
        this_job_info.config = config
        this_job_info.from_raw = from_raw is not None
        this_job_info.src_cube_paths = these_cubes
        this_job_info.src_cube_local_coords = these_cubes_local_coords
        this_job_info.cube_edge_len = cube_edge_len
        this_job_info.source_dtype = source_dtype

        # out_path = out_path.replace('mag'+str(src_mag), 'mag'+str(trg_mag))
        extension = ".jpg"
        if zanisotrop: # TODO
            this_job_info.trg_cube_path = get_cube_fname(dataset_base_path, experimentname,
                trg_mag, cur_x // 2, cur_y // 2, cur_z, extension)  #d int/int
            this_job_info.trg_cube_path2 = get_cube_fname(dataset_base_path, experimentname,
                trg_mag, cur_x // 2, cur_y // 2, cur_z + 1, extension)  #d int/int
        else:
            this_job_info.trg_cube_path = get_cube_fname(dataset_base_path, experimentname,
                trg_mag, cur_x // 2, cur_y // 2, cur_z // 2, extension)  #d int/int

        if skip_already_cubed_downsample_layers and (\
                os.path.exists(this_job_info.trg_cube_path) and
                #os.path.exists(this_job_info.trg_cube_path.replace("png", "jpg"))\
                (not zanisotrop or os.path.exists(this_job_info.trg_cube_path2))):
                #and os.path.exists(this_job_info.trg_cube_path2.replace("png", "jpg"))):
            #log_fn("path exists: {0}".format(this_job_info.trg_cube_path.replace("png", "{png,jpg}")))
            skipped_count += 1
        else:
            #log_fn("not path exists: {0}".format(this_job_info.trg_cube_path))
            #log_fn("not path exists: {0}".format(this_job_info.trg_cube_path2))
            downsampling_job_info.append(this_job_info)

    # Split up the downsampling into chunks that can be hold in memory. This
    # allows us to separate reading and writing from the storage,
    # often massively increasing the IO performance

    if len(downsampling_job_info) > buffer_size_in_cubes_downsampling:
        # split the jobs in chunks

        # how many chunks do we need?
        scl = 2 if zanisotrop else 1
        chunks_required = int(np.ceil(len(downsampling_job_info)*scl / buffer_size_in_cubes_downsampling))

        chunked_jobs = np.array_split(downsampling_job_info, chunks_required)
        # convert back to python list
        chunked_jobs = [el.tolist() for el in chunked_jobs]
    else:
        chunked_jobs = [downsampling_job_info]

    log_fn("{0} ds jobs to run in {1} chunks, {2} skipped, {3} total".format(len(downsampling_job_info),
           len(chunked_jobs), skipped_count, len(downsampling_job_info) + skipped_count))

    if len(downsampling_job_info) == 0:
        return True

    log_queue = mp.Queue()

    job_prep_time = time.time() - job_prep_time
    log_fn("Job preparation took {0:.2f} s".format(job_prep_time))

    worker_pool = mp.Pool(num_workers, downsample_cube_init, [log_queue], maxtasksperchild=num_workers)

    for chunk_id, this_job_chunk in enumerate(chunked_jobs):
        log_fn('Memory usage {0} MB'.format(get_mem_usage()))

        chunk_time = time.time()

        log_fn("Starting {0} workers...".format(num_workers))
        log_fn("First cube (of {0}) in chunk {1} (of {2}): {3}"
               .format(len(this_job_chunk), chunk_id, len(chunked_jobs), this_job_chunk[0].trg_cube_path))

        ref_time = time.time()

        log_fn("Downsampling…")
        cubes = worker_pool.map(downsample_cube, this_job_chunk, chunksize=10)
        #worker_pool.close()

        while not log_queue.empty():
            log_output = log_queue.get()
            log_fn(log_output)
        #cubes = map(downsample_cube, this_job_chunk)

        #worker_pool.join()

        log_fn("Downsampling took {0:.2f} s (on avg per cube {1} s)"
               .format(time.time() - ref_time
               , (time.time() - ref_time) / len(this_job_chunk))) #d float/int

        log_fn("Writing (and compressing)…")
        write_workers = []
        cnt_write_workers = 0
        cube_write_time = time.time()
        nskipped_cubes = [0,0]
        # start writing the cubes
        for cube_data, job_info in zip(cubes, this_job_chunk):
            # Changed this bogus thing here to the skip count in downsampling,
            #   so that completely all zero downsamnpled blocks are also skipped.
            #   cube_data is returned as None in those cases.
            # for chunk in this_job_chunk:
            #     if chunk.trg_cube_path != 'bogus':
            #         prefix = os.path.dirname(chunk.trg_cube_path)
            #         break
            if cube_data is None:
                #print("Skipped cube {0}".format(job_info.trg_cube_path))
                nskipped_cubes[0] += 1; nskipped_cubes[1] += 1
                continue

            if job_info.trg_cube_path2:
                first_cube = cube_data[:cube_edge_len, :, :]
                second_cube = cube_data[cube_edge_len:, :, :]
            else:
                first_cube = cube_data
                second_cube = None

            if cnt_write_workers > num_write_workers:
                d = write_queue.get()
                nskipped_cubes[d['icube']] += d['nskipped']
                write_workers[d['iworker']].join()
                write_workers[d['iworker']].close()
                cnt_write_workers -= 1

            cur_worker = mp.Process(target=write_compressed_cube, args=(config, first_cube,
                os.path.dirname(job_info.trg_cube_path), job_info.trg_cube_path,
                False, write_queue, len(write_workers), 0))
            cur_worker.start()
            write_workers.append(cur_worker)
            cnt_write_workers += 1

            if job_info.trg_cube_path2:
                cur_worker = mp.Process(target=write_compressed_cube, args=(config, second_cube,
                    os.path.dirname(job_info.trg_cube_path2), job_info.trg_cube_path2,
                    False, write_queue, len(write_workers), 1))
                cur_worker.start()
                write_workers.append(cur_worker)
                cnt_write_workers += 1
        # for cube_data, job_info in zip(cubes, this_job_chunk):

        # wait until all writes are finished
        for i in range(cnt_write_workers):
            d = write_queue.get()
            nskipped_cubes[d['icube']] += d['nskipped']
            write_workers[d['iworker']].join()
            write_workers[d['iworker']].close()
        write_workers = []

        cube_write_time = time.time() - cube_write_time
        chunk_time = time.time() - chunk_time

        if len(write_workers) > 0:
            log_fn("Writing {0} cubes took {1:.2f} s (on avg {2} s per cube)"
                   .format(len(write_workers), cube_write_time, cube_write_time / len(write_workers)))
            log_fn("Processing chunk took {0:.2f} s (on avg per cube {1} s)"
                   .format(chunk_time, chunk_time / len(this_job_chunk)))
            log_fn("Skipped {0} empty first and {1} empty second cubes".format(nskipped_cubes[0], nskipped_cubes[1]))
        else:
            log_fn("Skipped complete chunk in {0:.6f} s".format(chunk_time))

        for x in cubes: del x
        del first_cube, second_cube, cube_data, cubes, job_info, this_job_chunk

    worker_pool.close()
    worker_pool.join()

    return True


def downsample_cube(job_info):
    """TODO

    Args:
        job_info (downsample_job_info):
            An object that holds data required for downsampling.
    """

    # the first cube in the list contains the new coordinate of the created
    # downsampled out-cube

    cube_edge_len = job_info.cube_edge_len

    down_block = np.zeros([cube_edge_len*2, cube_edge_len*2, cube_edge_len*2],
                          dtype=job_info.source_dtype)

    if FADVISE_AVAILABLE:
        for src_path in job_info.src_cube_paths:
            fadvise.willneed(src_path)

        #time.sleep(0.2)

    skipped_count = 0
    for path_to_src_cube, src_coord in zip(job_info.src_cube_paths,
                                           job_info.src_cube_local_coords):
        if path_to_src_cube == 'bogus':
            continue

        if job_info.from_raw:
            # Yes, I know the numpy fromfile function - this is significantly
            # faster on our cluster
            #content = ''
            #buffersize=131072*2
            fd = io.open(path_to_src_cube, 'rb')
            #             # buffering = buffersize)
            # for i in range(0, (cube_edge_len**3 / buffersize) + 1):
            #    content += fd.read(buffersize)
            #content = fd.read(-1)
            this_cube = np.frombuffer(fd.read(-1), dtype=job_info.source_dtype)
            fd.close()
            # this_cube = np.fromfile(path_to_src_cube, dtype=job_info.source_dtype)
        else:
            try:
                with Image.open(path_to_src_cube) as load_image:
                    this_cube = np.array(load_image)
            except:
                skipped_count += 1
                continue

        try:
            this_cube = this_cube.reshape([cube_edge_len, cube_edge_len, cube_edge_len])
        except Exception as e:
            print(path_to_src_cube)
            raise e

        if job_info.config.getboolean('Processing', 'compress_source_downsampling_mag', fallback=False)\
                and job_info.config.getint('Processing', 'first_downsampling_mag') == job_info.trg_mag:

            this_job_info = CompressionJobInfo()
            this_job_info.compressor = job_info.config.get('Compression', 'compression_algo')
            this_job_info.quality_or_ratio = job_info.config.getint('Compression', 'out_comp_quality')
            this_job_info.src_cube_path = path_to_src_cube
            this_job_info.pre_gauss = job_info.config.getfloat('Compression', 'pre_comp_gauss_filter')
            compress_cube(this_job_info, this_cube)

        # NO swap here!
        # new cuber removed 'F' order allocation and loading above, so swap is no longer necessary.
        #this_cube = np.swapaxes(this_cube, 0, 2)
        #this_cube = np.swapaxes(this_cube, 1, 2)

        down_block[src_coord[2]*cube_edge_len:src_coord[2]*cube_edge_len + cube_edge_len,
                   src_coord[1]*cube_edge_len:src_coord[1]*cube_edge_len + cube_edge_len,
                   src_coord[0]*cube_edge_len:src_coord[0]*cube_edge_len + cube_edge_len]\
            = this_cube

        #down_block = np.swapaxes(down_block, 0, 1)
    #raise()
    del this_cube

    if skipped_count == 8:
        return None

    # It is not clear to me whether this zooming function does actually the
    # right thing. One should
    # first filter the data and then
    # re-sample to avoid aliasing. The prefilter setting is possibly not
    # working correctly, as the scipy documentation appears to be not in
    # agreement with the actual source code, so that pre-filtering is only
    # triggered for orders > 1, even if set to True. I assume that bilinear
    # or higher order re-sampling itself is "filtering" and is "good
    # enough" for our case.
    # This website by Stephan Saalfeld has some interesting details,
    # but there is a ton of material coming from the photography community.
    # http://fiji.sc/Downsample
    # My personal experience: Avoid nearest neighbor (ie naive
    # re-sampling without any filtering), especially
    # for noisy images. On top of that, the gains of more sophisticated
    # filters become less clear, and data and scaling factor dependent.
    if job_info.trg_cube_path2:
        zoom = [1.0, 0.5, 0.5]
    else:
        zoom = 0.5
    down_block = scipy.ndimage.interpolation.zoom(
        down_block, zoom,
        output=job_info.source_dtype,
        # 1: bilinear
        # 2: bicubic
        order=1,
        # this does not mean nearest interpolation, it corresponds to how the
        # borders (out of bounds) are treated.
        mode='nearest'
        # cval=0.0,
        )

    # for i in range(0, down_block.shape[0]):
    #     down_block[i] = scipy.ndimage.gaussian_filter(
    #         down_block[i],
    #         sigma=0.35 * 0.5,
    #         mode='mirror')
    # down_block = down_block[:, ::2, ::2]

    # down_block = skimage.transform.resize(down_block,
    #                                       output_shape=[256, 128, 128],
    #                                       mode='symmetric',
    #                                       preserve_range=True,
    #                                       #anti_aliasing=True
    # ).astype(job_info.source_dtype)

    ## use block-reduce method
    #block_size = (1,2,2) if job_info.trg_cube_path2 else (2,2,2)
    ##down_block = measure.block_reduce(down_block, block_size=block_size,
    ##    func=np.median).astype(job_info.source_dtype)
    #down_block = measure.block_reduce(down_block, block_size=block_size,
    #    func=np.mean).astype(job_info.source_dtype)

    # extract directory of out_path
    #if not os.path.exists(os.path.dirname(job_info.trg_cube_path)):
    #    os.makedirs(os.path.dirname(job_info.trg_cube_path))

    #down_block.tofile(job_info.trg_cube_path)

    return down_block


def downsample_cube_init(log_queue):
    """TODO
    """

    downsample_cube.log_queue = log_queue


def compress_dataset(config, log_fn):
    """TODO
    """

    dataset_base_path = config.get('Project', 'target_path')
    num_workers = config.getint('Compression', 'num_compression_cores')

    log_fn("Analysing source dataset...")

    list_of_all_cubes = []
    for mag_dir in find_mag_folders(dataset_base_path, log_fn):
        if mag_dir < 2:# HACK
            continue
        list_of_all_cubes.extend(
            get_list_of_all_cubes_in_dataset(os.path.join(dataset_base_path, "mag{}".format(mag_dir)), log_fn)[1])

    compress_job_infos = []
    for cube_path in list_of_all_cubes:
        this_job_info = CompressionJobInfo()

        this_job_info.compressor = config.get('Compression', 'compression_algo')
        this_job_info.quality_or_ratio = config.getint('Compression', 'out_comp_quality')
        this_job_info.src_cube_path = cube_path
        this_job_info.pre_gauss = config.getfloat('Compression', 'pre_comp_gauss_filter')

        compress_job_infos.append(this_job_info)

    log_fn("Starting {0} workers...".format(num_workers))
    log_queue = mp.Queue()

    worker_pool = mp.Pool(num_workers, compress_cube_init, [log_queue])
    # distribute cubes to worker pool
    #async_result = worker_pool.map(compress_cube, compress_job_infos, chunksize=10)
    worker_pool.map(compress_cube, compress_job_infos, chunksize=10)
    worker_pool.close()

    #while not async_result.ready():
    while not log_queue.empty():
        log_output = log_queue.get()
        log_fn(log_output)

    worker_pool.join()

    log_fn("Done compressing…".format(num_workers))


# xxx - compression has not been recently tested in this version of the cuber.
def compress_cube(job_info, cube_raw = None):
    """TODO
    """

    #ref_time = time.time()
    cube_edge_len = job_info.cube_edge_len
    open_jpeg_bin_path = job_info.open_jpeg_bin_path

    if 'jpg' in job_info.compressor:
        if job_info.quality_or_ratio < 40:
            raise Exception("Improbable quality value set for jpg as "
                            "compressor: Use values between 50 and 90 for "
                            "reasonable results. "
                            "Higher value -> better quality.")
    elif 'j2k' in job_info.compressor:
        if job_info.quality_or_ratio > 20:
            raise Exception("Improbable quality value set for j2k as "
                            "compressor: Use values between 2 and 10 for "
                            "reasonable results. "
                            "Lower value -> better quality.")

    cube_path_without_ending = os.path.splitext(job_info.src_cube_path)[0]

    if FADVISE_AVAILABLE:
        fadvise.willneed(job_info.src_cube_path)

    if cube_raw is None:
        if job_info.src_cube_path.endswith(".raw"):
            content = ''
            # buffersize=131072*2
            fd = io.open(job_info.src_cube_path, 'rb')
            #             # buffering = buffersize)
            # for i in range(0, (cube_edge_len**3 / buffersize) + 1):
            #    content += fd.read(buffersize)
            content = fd.read(-1)
            fd.close()
            cube_raw = np.frombuffer(content, dtype=np.uint8)
            #cube_raw = np.fromfile(job_info.src_cube_path, dtype=np.uint8)
        elif job_info.src_cube_path.endswith(".png"):
            with Image.open(job_info.src_cube_path) as load_image:
                cube_raw = np.array(load_image)
    cube_raw = cube_raw.reshape(cube_edge_len * cube_edge_len, -1)

    if job_info.pre_gauss > 0.0:
        # blur only in 2d
        if CV2_AVAILABLE:
            cv2.GaussianBlur(cube_raw,
                             (5, 5),
                             job_info.pre_gauss,
                             cube_raw)
        else:
            cube_raw = scipy.ndimage.filters.gaussian_filter(
                cube_raw, job_info.pre_gauss)

    cube_img = Image.fromarray(cube_raw)

    if 'jpg' in job_info.compressor:
        # the exact shape of the 2d representation for compression is
        # actually important!
        # PIL performs reasonably fast; one could try libjpeg-turbo to make
        # it even faster, but IO is the bottleneck anyway
        cube_img.save(cube_path_without_ending + '.jpg', quality=job_info.quality_or_ratio)

    if 'png' in job_info.compressor:
        cube_img.save(cube_path_without_ending + '.png')

    if 'j2k' in job_info.compressor:
        cmd_string = open_jpeg_bin_path + \
                     ' -i ' + job_info.src_cube_path +\
                     ' -o ' + cube_path_without_ending + '.jp2' +\
                     ' -r ' + str(job_info.quality_or_ratio) +\
                     ' -b 64,64 -s 1,1 -n 3 ' +\
                     ' -F ' + str(cube_edge_len) + ',' +\
                     str(cube_edge_len*cube_edge_len) + ',1,8,u'
        os.system(cmd_string)

    # print here, not log_fn, because log_fn may not be able to write to some
    # data structure from multiple processes.
    #compress_cube.log_queue.put("Compress, writing of {0} took: {1} s"\
    #    .format(cube_path_without_ending, time.time() - ref_time))

    return


def compress_cube_init(log_queue):
    """TODO
    """

    compress_cube.log_queue = log_queue


def write_compressed_cube(config, cube_data, prefix, cube_full_path, transpose, write_queue, iworker, icube):
    if not os.path.exists(prefix):
        os.makedirs(prefix)

    this_job_info = CompressionJobInfo()

    this_job_info.compressor = config.get('Compression', 'compression_algo')
    this_job_info.quality_or_ratio = config.getint('Compression', 'out_comp_quality')
    this_job_info.src_cube_path = cube_full_path
    this_job_info.pre_gauss = config.getfloat('Compression', 'pre_comp_gauss_filter')

    if 'raw' in this_job_info.compressor:
        write_cube(cube_data, prefix, cube_full_path, transpose, write_queue, iworker, icube)
    if this_job_info.compressor != 'raw':
        compress_cube(this_job_info, cube_data)
        d = {'nskipped':0, 'iworker':iworker, 'icube':icube}
        write_queue.put(d)


def write_cube(cube_data, prefix, cube_full_path, transpose, write_queue, iworker, icube):
    """TODO
    """

    if np.sum(cube_data) > 0:
        was_skipped = False
        if transpose: cube_data = cube_data.T

        #ref_time=time.time()
        if not os.path.exists(prefix):
            os.makedirs(prefix)

        try:
            cube_data.tofile(os.path.splitext(cube_full_path)[0] + '.raw')
            #print("writing took: {0}s".format(time.time()-ref_time))
        except IOError:
            # no log_fn due to multithreading
            print("Could not write cube: {0}".format(cube_full_path))
    else:
        was_skipped = True

    d = {'nskipped':int(was_skipped), 'iworker':iworker, 'icube':icube}
    write_queue.put(d)


def _natural_sort_key(s, _nsre=re.compile('([0-9]+)')):
    """Key function for natural sorting.

    E.g. this orders
    '1.tif' < '2.tif' < ... < '10.tif' < '11.tif'
    instead of the standard sort() order
    '1.tif' < '10.tif' < '11.tif' < ... < '2.tif'

    Fixes https://github.com/knossos-project/knossos_cuber/issues/3

    http://stackoverflow.com/a/16090640
    """
    return [int(text) if text.isdigit() else text.lower()
            for text in re.split(_nsre, s)]


def init_from_source_dir(config, log_fn):
    """Compute certain cubing parameters from the set of parameters
    specified by the user.

    Args:
        config (ConfigParser):
            See `read_config_file()' or `knossos_cuber/config.ini'

        log_fn (function): function with str parameter

    Returns:
        num_x_cubes_per_pass (int):
        num_y_cubes (int):
        num_z_cubes (int):
        all_source_files ([str*]):
        num_passes_per_cube_layer (int):
    """


    source_format = config.get('Dataset', 'source_format')
    source_path = config.get('Project', 'source_path')
    source_channel = config.get('Dataset', 'source_channel')

    source_files = [
        f for f in os.listdir(source_path)
        if any([f.endswith(suffix)
            ## [:-1] cuts away the description string `*.suffix'
            #for suffix in SOURCE_FORMAT_FILES[source_format][:-1]])]
            # seems that more than one wildcard specifier was subsequently added to SOURCE_FORMAT_FILES.
            #   just remove this, a filename could not / should not contain an asterix anyways.
            for suffix in SOURCE_FORMAT_FILES[source_format]])]


    source_path = config.get('Project', 'source_path')
    all_source_files = [source_path + '/' + s for s in source_files]

    if all_source_files == []:
        print("No image files of format " + source_format + " was found.")
        sys.exit()


    all_source_files.sort(key=_natural_sort_key)


    num_z = len(all_source_files)

    # open the first image and extract the relevant information - all images are
    # assumed to have equal dimensions!
    same_knossos_as_tif_stack_xy_orientation = \
        config.getboolean('Dataset', 'same_knossos_as_tif_stack_xy_orientation')
    if source_format == 'hdf5':
        source_dims, source_dtype = big_img_info(all_source_files[0])
        if same_knossos_as_tif_stack_xy_orientation:
            source_dims = source_dims[::-1]
    else:
        log_fn("Loading first image to get dimensions"); t = time.time()
        with Image.open(all_source_files[0]) as load_image:
            test_data = np.array(load_image)
        # allow cubing of one channel of color data
        if test_data.ndim > 2: test_data = test_data[:,:,source_channel]
        # knossos uses swapped xy axes relative to images
        if same_knossos_as_tif_stack_xy_orientation: test_data = test_data.T
        source_dims = test_data.shape
        source_dtype = test_data.dtype
        log_fn("\t done in {:.2f} s".format(time.time()-t,))

    config.set('Dataset', 'source_dims', str(source_dims))
    config.set('Dataset', 'source_dtype', str(source_dtype))

    #q (important for division below!) Why getfloat, not getint?
    #  It is int in the config.ini and that would make more sense.
    cube_edge_len = config.getfloat('Processing', 'cube_edge_len')

    # determine the number of passes required for each cube layer - if several
    # passes are required, we split the xy plane up in X cube_edge_len chunks,
    # always with full y height
    num_x_cubes = int(math.ceil(source_dims[0] / cube_edge_len)) #d int/float
    num_y_cubes = int(math.ceil(source_dims[1] / cube_edge_len)) #d int/float
    num_z_cubes = int(math.ceil(num_z / cube_edge_len)) #d int/float

    buffer_size_in_cubes = config.getint('Processing', 'buffer_size_in_cubes')

    log_fn("Dataset is %d x %d x %d voxels, %d x %d x %d knossos cubes" % \
          (source_dims[0], source_dims[1], num_z, num_x_cubes, num_y_cubes, num_z_cubes))
    if num_x_cubes * num_y_cubes < buffer_size_in_cubes:
        log_fn("Buffer size sufficient for a single pass per z cube layer")
        num_passes_per_cube_layer = 1
        num_x_cubes_per_pass = num_x_cubes
    else:
        log_fn("Buffer size not sufficient for single pass per z cube layer - "
               "either increase the buffer size or accept the longer cubing "
               "time due to IO overhead.")
        log_fn("\tadjusting buffer_size_in_cubes from %d to multiple"
               " of number of y cubes" % (buffer_size_in_cubes,))
        buffer_size_in_cubes = \
            int(math.ceil(buffer_size_in_cubes / num_y_cubes)) * num_y_cubes
        log_fn("\tnew buffer_size_in_cubes %d" % (buffer_size_in_cubes,))
        num_x_cubes_per_pass = buffer_size_in_cubes // num_y_cubes

        num_passes_per_cube_layer = \
            int(math.ceil(num_x_cubes / num_x_cubes_per_pass))
        log_fn("\trequires %d passes per cube layer, %d xcubes per pass" % \
               (num_passes_per_cube_layer,num_x_cubes_per_pass))

    CubingInfo = namedtuple('CubingInfo',
                            'num_x_cubes_per_pass num_y_cubes num_z_cubes '
                            'all_source_files num_passes_per_cube_layer')

    cube_info = CubingInfo(num_x_cubes_per_pass,
                           num_y_cubes,
                           num_z_cubes,
                           all_source_files,
                           num_passes_per_cube_layer)

    return cube_info


def read_zslice(iworker, local_z, source_format, source_file, transpose,
        source_channel, block_ranges, xpass_range, read_queue, this_layer_out_block):
    """TODO
    """
    #print("worker {} started".format(iworker)); t = time.time()
    if source_format != 'hdf5':
        # xxx - could implement a cacheing mechanism here, copy to /dev/shm
        with Image.open(source_file) as load_image:
            this_layer = np.array(load_image)
        # allow cubing of one channel of color data
        if this_layer.ndim > 2: this_layer = this_layer[:,:,source_channel]
        if transpose: this_layer = this_layer.T

    # the blocking is a throwback for using queues (2GB item limit) instead of shared memory.
    b = block_ranges
    for x in range(len(b[0])):
        sx = b[0][x][1] - b[0][x][0]
        for y in range(len(b[1])):
            # only assign the portion of the layer that we need,
            #   in the case that we have to do multiple passes per layer.
            if b[0][x][0] < xpass_range[1] and b[0][x][1] > xpass_range[0]:
                bdx = [max(b[0][x][0],xpass_range[0]), min(b[0][x][1],xpass_range[1])]
                bdx = [x - xpass_range[0] for x in bdx]
                bcx = [(xpass_range[0] - b[0][x][0]) if b[0][x][0] < xpass_range[0] else 0,
                    (sx - b[0][x][1] + xpass_range[1]) if b[0][x][1] > xpass_range[1] else sx]
                if source_format == 'hdf5':
                    if transpose:
                        rng = [[b[1][y][0],b[1][y][1]], [bcx[0],bcx[1]]]
                    else:
                        rng = [[bcx[0],bcx[1]], [b[1][y][0],b[1][y][1]]]
                    chunk = big_img_load(source_file, rng=rng)
                    if transpose: chunk = chunk.T
                else:
                    chunk = this_layer[bcx[0]:bcx[1],b[1][y][0]:b[1][y][1]]
                this_layer_out_block[iworker + local_z, bdx[0]:bdx[1], b[1][y][0]:b[1][y][1]] = chunk
                if x==len(b[0])-1 and y==len(b[1])-1:
                    # zero-fill the remainder of the block
                    this_layer_out_block[iworker + local_z, bdx[1]:, b[1][y][1]:] = 0
            #if b[0][x][0] < xpass_range[1] and b[0][x][1] > xpass_range[0]:
            d = {'iworker':iworker,}
            read_queue.put(d)
        #for y in range(len(b[1])):
    #for x in range(len(b[0])):
    #print("worker {} finished in {}".format(iworker, time.time()-t))


def make_mag1_cubes_from_z_stack(config,
                                 all_source_files,
                                 num_x_cubes_per_pass,
                                 num_y_cubes,
                                 num_z_cubes,
                                 num_passes_per_cube_layer,
                                 log_fn):
    """TODO
    """

    exp_name = config.get('Project', 'exp_name')
    target_path = config.get('Project', 'target_path') + "/" + exp_name
    config.set('Project', 'target_path', target_path)

    cube_edge_len = config.getint('Processing', 'cube_edge_len')
    # use this instead of skip_already_cubed_layers in order to parallelize / restart.
    z_cubes_mag1_rng = literal_eval(config.get('Processing', 'z_cubes_mag1_rng'))
    if z_cubes_mag1_rng[0] < 0: z_cubes_mag1_rng[0] = 0
    if z_cubes_mag1_rng[1] < 0: z_cubes_mag1_rng[1] = num_z_cubes
    assert(z_cubes_mag1_rng[0] < z_cubes_mag1_rng[1] and z_cubes_mag1_rng[1] <= num_z_cubes)

    source_dtype = config.get('Dataset', 'source_dtype')
    if source_dtype == 'uint16':
        source_dtype = np.uint16
        source_bytes = 2
        #source_ctype = 'H'
    else:
        source_dtype = np.uint8
        source_bytes = 1
        #source_ctype = 'B'
    source_shape = literal_eval(config.get('Dataset', 'source_dims'))
    source_format = config.get('Dataset', 'source_format')
    source_channel = config.get('Dataset', 'source_channel')

    num_z = len(all_source_files)
    rmd_z = num_z % cube_edge_len

    same_knossos_as_tif_stack_xy_orientation = \
        config.getboolean('Dataset', 'same_knossos_as_tif_stack_xy_orientation')

    # the blocking is a throwback for using queues (2GB item limit) instead of shared memory.
    #max_size = 2**31-1
    max_size = None
    size = source_shape[0]*source_shape[1]*source_bytes
    if max_size is None or size < max_size:
        nblks = [1,1]
    else:
        # create number of blocks in each dimension that is proportional to their sizes
        ntblks = np.ceil(size / max_size)
        if source_shape[0] > source_shape[1]:
          nblks = int(np.ceil(np.sqrt(ntblks*source_shape[1]/source_shape[0])))
          nblks = [int(np.ceil(ntblks/nblks)), nblks]
        else:
          nblks = int(np.ceil(np.sqrt(ntblks*source_shape[0]/source_shape[1])))
          nblks = [nblks, int(np.ceil(ntblks/nblks))]
    block_ranges = [[[x[0],x[-1]+1] for x in np.array_split(np.arange(y), z)] \
        for y,z in zip(source_shape, nblks)]
    ntblks = nblks[0]*nblks[1]

    num_read_workers = config.getint('Processing', 'num_read_workers')
    num_write_workers = config.getint('Processing', 'num_write_workers')

    # this is for generating mags starting with isotropic voxels.
    # this allows for a dataset with mags other than 2x downsamplings to be created.
    # this is to get as close as possible to isotropic voxels using integer downsampling.
    first_mag = config.getint('Processing', 'first_mag')

    # inits for multiprocessing in the z-loop
    write_queue = mp.Queue(num_write_workers)
    read_queue = mp.Queue(num_read_workers)
    # use shared memory for parallel reads
    # NOTE: by default this uses /dev/shm, which by default is only half as big as system memory... meh
    block_shape = [cube_edge_len,
                   num_x_cubes_per_pass * cube_edge_len,
                   num_y_cubes * cube_edge_len]
    block_size = cube_edge_len**3 * num_x_cubes_per_pass * num_y_cubes
    smm = managers.SharedMemoryManager()
    smm.start()  # Start the process that manages the shared memory blocks
    shm = smm.SharedMemory(size=block_size*source_bytes)
    this_layer_out_block = np.ndarray(block_shape, dtype=source_dtype, buffer=shm.buf)
    ## use ctypes rawarray for parallel reads, this is slower.
    #this_layer_out_block = np.frombuffer(mp.RawArray(source_ctype, block_size),
    #        dtype=source_dtype).reshape(block_shape)

    # we iterate over the z cubes and handle cube layer after cube layer
    num_x_cubes = num_x_cubes_per_pass*num_passes_per_cube_layer
    for cur_z in range(z_cubes_mag1_rng[0], z_cubes_mag1_rng[1]):
        log_fn("Cubing for z cube layer {} / {}".format(cur_z, num_z_cubes))
        for cur_pass in range(0, num_passes_per_cube_layer):
            xpass_range = [cur_pass * num_x_cubes_per_pass * cube_edge_len,
                           (cur_pass+1) * num_x_cubes_per_pass * cube_edge_len]
            if xpass_range[1] > source_shape[0]: xpass_range[1] = source_shape[0]

            log_fn("Loading source files for pass {0}/{1}".format(cur_pass, num_passes_per_cube_layer))
            ref_time = time.time()
            for local_z in range(0, cube_edge_len, num_read_workers):
                z = cur_z*cube_edge_len + local_z
                if z >= num_z: break

                if cur_z == num_z_cubes-1 and rmd_z > 0 and local_z + num_read_workers > rmd_z:
                    io_workers = rmd_z - local_z
                elif local_z + num_read_workers > cube_edge_len:
                    io_workers = cube_edge_len - local_z
                else:
                    io_workers = num_read_workers
                log_fn("\tReading {} slices in parallel workers, {}/{}".format(io_workers, local_z, cube_edge_len))
                read_time = time.time()

                # threading does not work with hdf5, a global lock gets applied even for different hdf5 files.
                # https://github.com/h5py/h5py/issues/591
                read_workers = [None]*io_workers
                for i in range(io_workers):
                    read_workers[i] = mp.Process(target=read_zslice, args=(i, local_z, source_format,
                           all_source_files[z+i], same_knossos_as_tif_stack_xy_orientation, source_channel,
                           block_ranges, xpass_range, read_queue, this_layer_out_block))
                    read_workers[i].start()
                # NOTE: only call join after queue is emptied

                dt = time.time()
                print_every = io_workers*ntblks # to disable prints here
                worker_cnts = np.zeros((io_workers,), dtype=np.int64)
                for i in range(io_workers*ntblks):
                    if i>0 and i%print_every==0:
                        print('%d through q in %.3f s, worker_cnts:' % (print_every, time.time()-dt,))
                        print(worker_cnts); dt = time.time()
                    d = read_queue.get()
                    worker_cnts[d['iworker']] += 1
                assert(read_queue.empty())
                [x.join() for x in read_workers]
                [x.close() for x in read_workers]

                log_fn("\tParallel reading took {:.2f} s".format(time.time() - read_time))
            log_fn("Reading took {0:.2f} s".format(time.time() - ref_time))
            # zero-fill remainder slices in last cube
            if cur_z == num_z_cubes-1 and rmd_z > 0:
                this_layer_out_block[rmd_z:,:,:] = 0


            # write out the cubes for this z-cube layer and buffer
            log_fn("Writing cubes in {} parallel workers".format(num_write_workers))
            twrite = time.time()
            write_workers = []
            cnt_write_workers = 0
            nskipped_cubes = 0
            for cur_x in range(0, num_x_cubes_per_pass):
                x_start = cur_x*cube_edge_len
                x_end = (cur_x+1)*cube_edge_len
                glob_cur_x_cube = cur_x + cur_pass * num_x_cubes_per_pass
                glob_cur_z_cube = cur_z
                log_fn("Writing y cubes for x={0}/{1}, z={2}/{3}"\
                       .format(glob_cur_x_cube, num_x_cubes, cur_z, num_z_cubes))
                for cur_y in range(0, num_y_cubes):
                    ref_time = time.time()
                    glob_cur_y_cube = cur_y

                    # slice cube_data out of buffer
                    y_start = cur_y*cube_edge_len
                    y_end = (cur_y+1)*cube_edge_len
                    cube_data = this_layer_out_block[:, x_start:x_end, y_start:y_end]

                    prefix = os.path.normpath(os.path.abspath(
                        target_path + ('/mag%d' % (first_mag,)) + ('/x%04d/y%04d/z%04d/'
                        % (glob_cur_x_cube, glob_cur_y_cube, glob_cur_z_cube)) ))

                    cube_full_path = os.path.normpath(
                        prefix + '/%s_mag%d_x%04d_y%04d_z%04d.raw'
                        % (exp_name,
                           first_mag,
                           glob_cur_x_cube,
                           glob_cur_y_cube,
                           glob_cur_z_cube))

                    if cnt_write_workers > num_write_workers:
                        d = write_queue.get()
                        nskipped_cubes += d['nskipped']
                        write_workers[d['iworker']].join()
                        write_workers[d['iworker']].close()
                        cnt_write_workers -= 1

                    cur_worker = mp.Process(target=write_cube, args=(cube_data, prefix, cube_full_path,
                            same_knossos_as_tif_stack_xy_orientation, write_queue, len(write_workers), 0))
                    cur_worker.start()
                    write_workers.append(cur_worker)
                    cnt_write_workers += 1
                #for cur_y in range(0, num_y_cubes):
            #for cur_x in range(0, num_x_cubes_per_pass):

            # wait until all writes are finished
            for i in range(cnt_write_workers):
                d = write_queue.get()
                nskipped_cubes += d['nskipped']
                write_workers[d['iworker']].join()
                write_workers[d['iworker']].close()
            write_workers = []

            log_fn("Writing took {0:.2f} s".format(time.time()-twrite))
            log_fn("Skipped {0} empty cubes".format(nskipped_cubes))

        #for cur_pass in range(0, num_passes_per_cube_layer):
    #for cur_z in range(0, num_z_cubes):
    smm.shutdown()  # Calls unlink() on sl, raw_shm, and another_sl


def knossos_cuber(config, log_fn):
    """Cube a dataset.

    Args:
        config (ConfigParser):
            A configuration object created by ConfigParser.
            See `knossos_cuber/config.ini' and `read_config_file()'
            for more information about the parameters.

        log_fn (function):
            Function with str parameter that processes log/debug output.

    """

    try:
        boundaries = literal_eval(config.get('Dataset', 'boundaries'))
    except ValueError:
        boundaries = None

    perform_mag1_cubing = config.getboolean('Processing', 'perform_mag1_cubing')
    cubing_info = init_from_source_dir(config, log_fn)
    all_source_files = cubing_info.all_source_files

    exp_name = config.get('Project', 'exp_name')
    if perform_mag1_cubing:
        mag1_ref_time = time.time()
        make_mag1_cubes_from_z_stack(
            config,
            all_source_files,
            cubing_info.num_x_cubes_per_pass,
            cubing_info.num_y_cubes,
            cubing_info.num_z_cubes,
            cubing_info.num_passes_per_cube_layer,
            log_fn)
    else:
        log_fn("perform_mag1_cubing==False, skipping mag1 cubing")
        target_path = config.get('Project', 'target_path') + "/" + exp_name
        config.set('Project', 'target_path', target_path)

    source_dims = literal_eval(config.get('Dataset', 'source_dims'))
    boundaries = (source_dims[0], source_dims[1], len(all_source_files))
    config.set('Dataset', 'boundaries', str(boundaries))
    dataset_base_path = config.get('Project', 'target_path')
    scale = literal_eval(config.get('Dataset', 'scaling'))

    if perform_mag1_cubing:
        # this is for generating mags starting with isotropic voxels.
        # this allows for a dataset with mags other than 2x downsamplings to be created.
        # this is to get as close as possible to isotropic voxels using integer downsampling.
        first_mag = config.getint('Processing', 'first_mag')

        cube_edge_len = config.getint('Processing', 'cube_edge_len')
        write_knossos_conf(dataset_base_path + "/", scale, boundaries, exp_name, mag=1, cube_edge_len=cube_edge_len)
        # only write dummy for mag detection
        fh = open(dataset_base_path + ("/mag%d/knossos.conf" % (first_mag,)), 'w'); fh.close()

        total_mag1_time = time.time() - mag1_ref_time

        log_fn("Mag 1 succesfully cubed. Took {0:.4f} h".format(total_mag1_time/3600)) #d f/i

    knossos_mag_names = config.get('Dataset', 'mag_names', fallback="knossos").lower() == "knossos"
    if knossos_mag_names:
        log_fn("using KNOSSOS mag names (1, 2, 4, 8, 16…)")
    else:
        log_fn("using consecutive mag names (1, 2, 3, 4, 5…)")

    if config.getboolean('Processing', 'perform_downsampling'):
        total_down_ref_time = time.time()
        # q mags are always ints, right? (important for division below!)
        curr_mag = config.getint('Processing', 'first_downsampling_mag', fallback=2)

        # `mags_to_gen' is specified like `2**20' in the configuration file.
        # To parse this number, the string has to be split at `**',
        # and then evaluated.
        mags_to_gen_string = config.get('Dataset', 'mags_to_gen')
        mags_to_gen = reduce(lambda x, y: int(x) ** int(y),
                             mags_to_gen_string.split("**"))

        if not knossos_mag_names:
            mags_to_gen = int(np.log2(mags_to_gen))+1
        while curr_mag <= mags_to_gen:
            if knossos_mag_names:
                prev_mag = curr_mag // 2
            else:
                prev_mag = curr_mag - 1
            worked = downsample_dataset(config, prev_mag, curr_mag, log_fn) #d int/int

            if worked:
                log_fn("Mag {0} succesfully cubed.".format(curr_mag))
                if knossos_mag_names:
                    curr_mag *= 2
                else:
                    curr_mag += 1
            else:
                log_fn("Done with downsampling.")
                break

        log_fn("All mags generated. Took {0:.4f} h."
               .format((time.time() - total_down_ref_time)/3600))

    if config.getboolean('Compression', 'perform_compression'):
        total_comp_ref_time = time.time()
        compress_dataset(config, log_fn)

        log_fn("Compression done. Took {0:.4f} h."
               .format((time.time() - total_comp_ref_time)/3600))

    log_fn('All done.')


def validate_config(config):
    """Validates the configuration file by checking two conditions:

        1.  `boundaries' has to be non-empty whenever
            `perform_mag1_cubing' is False.

        2.  If `source_format' is `raw', then `source_dims' and
            `source_dtype' have to be non-empty.

    Args:
        config (ConfigParser):
            ConfigParser object holding all configuration parameters.

    Returns:
        True if configuration is alright.
    """

    # not true !!!
    #perform_mag1_cubing = config.getboolean('Processing',
    #                                        'perform_mag1_cubing')
    #if not perform_mag1_cubing and not config.get('Dataset', 'boundaries'):
    #    raise InvalidCubingConfigError("When starting from mag1 cubes, the "
    #                                   "dataset boundaries must be specified.")

    # This validation only takes place for RAW files.
    # However, support for RAW files is not implemented yet.
    if config.get('Dataset', 'source_format') == 'raw':
        if not config.get('Dataset', 'source_dims') \
           or not config.get('Dataset', 'source_dtype'):
            raise InvalidCubingConfigError("When reading from 2D RAW images, "
                                           "source image size and data type "
                                           "must be specified.")

    return True


def read_config_file(config_file):
    """Reads a config(.ini) file to get parameters for cubing.
    An example config.ini file, with an explanation for each parameter,
    can be found in knossos_cuber's installation directory.

    Args:
        config_file (str): filename of the configuration file.

    Returns:
        A ConfigParser object holding the contents of config_file.
    """

    config = ConfigParser(allow_no_value=True)

    try:
        with open(config_file) as fh: config.read_file(fh)
    except IOError:
        print("Could not open config file `" + config_file + "'.")
        print("An IOError has appeared. Please check whether the "
              "configuration file exists and permissions are set.")

        sys.exit()

    return config


def create_parser():
    """Creates a parser for command-line arguments.
    The parser can read 4 options:

        Optional arguments:

            --format, -f : image format of input files

            --config, -c : path to a configuration file

        Positional arguments:

            source_dir : path to input files

            target_dir : output path

    Args:

    Returns:
        An ArgumentParser object.
    """

    parser = argparse.ArgumentParser()

    parser.add_argument(
        'source_dir',
        help="Directory containing the input images.")

    parser.add_argument(
        'target_dir',
        help="Output directory for the generated dataset.")

    parser.add_argument(
        '--format', '-f',
        help="Specifies the format of the input images. "
             "Currently supported formats are: png, tif, jpg. "
             "The option `jpg' searches for all images matching "
             "*.jp(e)g, and *.JP(E)G (`tif' and `png' respectively).")

    parser.add_argument(
        '--keep_z_until_mag',
        help="Magnification until to do anisotropic downsampling (only xy).")

    parser.add_argument(
        '--config', '-c',
        help="A configuration file. If no file is specified, `config.ini' "
             "from knossos_cuber's installation directory is used. Note that "
             "you still have to specify the input/output directory and "
             "source format via the command line.",
        default='config.ini')

    parser.add_argument('--z-cubes-mag1-rng', nargs=2, type=int, default=[-1,-1],
        help='only cube this range of z-cubes in mag1 (default to all)')

    return parser


def validate_args(args):
    """Check whether the format specified from the command-line is a
    supported format.

    Args:
        args (ArgumentParser.args): Arguments collected by
                                    ArgumentParser.

    Returns:
        True if arguments are alright.
    """

    if args.format is not None and args.format not in SOURCE_FORMAT_FILES.keys():
        print("Error: " + args.format + " was not found in the list of supported formats!")
        return False

    return True


def main():
    PARSER = create_parser()
    ARGS = PARSER.parse_args()

    if not validate_args(ARGS):
        sys.exit()

    if ARGS.config is not None:
        config_file = "config.ini"
        if not os.path.isfile(config_file):
            shutil.copyfile(os.path.join(os.path.dirname(os.path.realpath(__file__)),
                "config.ini"), config_file)
    CONFIG = read_config_file(ARGS.config)

    CONFIG.set('Project', 'source_path', ARGS.source_dir)
    CONFIG.set('Project', 'target_path', ARGS.target_dir)
    CONFIG.set('Dataset', 'source_format', ARGS.format)
    if ARGS.keep_z_until_mag is not None:
        CONFIG.set('Processing', 'keep_z_until_mag', ARGS.keep_z_until_mag)
    #print('keep_z_until_mag', CONFIG['Processing']['keep_z_until_mag'])
    CONFIG.set('Processing', 'z_cubes_mag1_rng', str(ARGS.z_cubes_mag1_rng))

    if validate_config(CONFIG):
        knossos_cuber(CONFIG, lambda x: sys.stdout.write(str(x) + '\n'))


if __name__ == '__main__':
    # try to clear out shared memory files from crashed jobs before starting
    for fn in glob.glob('/dev/shm/*'):
        if os.path.isfile(fn):
            try: os.remove(fn)
            except: pass
        else:
            shutil.rmtree(fn, ignore_errors=True)

    main()
