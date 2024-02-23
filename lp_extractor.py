from collections import OrderedDict
import concurrent.futures
import difflib as dl
import json
import logging
from natsort import natsorted
import os
from pathlib import Path
import re
import shutil
import subprocess
from threading import Lock

from ccp import CCP
from ce import CE
from config import Config
import lp_utils
from templ import TemplateGen

class Extractor(Config):
    def __init__(self, bsc, bsc_filter, apply_patches, app, avoid_ext = ''):
        super().__init__(bsc, bsc_filter)

        if apply_patches and not self.get_patches_dir().exists():
            raise ValueError('--apply-patches specified without patches. Run get-patches!')
        self.apply_patches = apply_patches

        self.quilt_log = open(Path(self.get_patches_dir(), 'quilt.log'), 'w')
        self.quilt_log.truncate()

        self.total = 0
        self.make_lock = Lock()

        if app == 'ccp':
            self.runner = CCP(bsc, bsc_filter, avoid_ext)
        else:
            self.runner = CE(bsc, bsc_filter)

        self.app = app
        self.tem = TemplateGen(self.bsc_num, self.filter, self.app)

    def process(self, args):
        i, fname, cs, fdata = args

        sdir = self.get_sdir(cs)
        odir = self.get_odir(cs)

        # The header text has two tabs
        cs_info = cs.ljust(15, ' ')
        idx = f'({i}/{self.total})'.rjust(15, ' ')

        logging.info(f'{idx} {cs_info} {fname}')

        out_dir = self.get_work_dir(cs, fname, self.app)
        out_dir.mkdir(parents=True, exist_ok=True)

        # create symlink to the respective codestream file
        os.symlink(Path(sdir, fname), Path(out_dir, Path(fname).name))

        # Make can regenerate fixdep for each file being processed per
        # codestream, so avoid the TXTBUSY error by serializing the 'make -sn'
        # calls. Make is pretty fast, so there isn't a real slow down here.
        with self.make_lock:
            cmd = lp_utils.get_make_cmd(self.cc, out_dir, cs, fname, odir)

        args, lenv = self.runner.cmd_args(cs, fname, ','.join(fdata['symbols']), out_dir,
                                          fdata, cmd)

        with open(Path(out_dir, f'{self.app}.out.txt'), 'w') as f:
            # Write the command line used
            f.write('\n'.join(args) + '\n')
            f.flush()
            subprocess.run(args, cwd=odir, stdout=f, stderr=f, env=lenv,
                           check=True)

        self.codestreams[cs]['files'][fname]['ext_symbols'] = self.runner.get_symbol_list(out_dir)

        lp_out = Path(out_dir, self.lp_out_file(fname))

		# Remove the local path prefix of the klp-ccp generated comments
        # Open the file, read, seek to the beginning, write the new data, and
        # then truncate (which will use the current position in file as the
        # size)
        with open(str(lp_out), 'r+') as f:
            file_buf = f.read()
            f.seek(0)
            f.write(file_buf.replace(f'from {str(sdir)}/', 'from '))
            f.truncate()

        self.tem.CreateMakefile(cs, fname)

    def run(self):
        logging.info(f'Work directory: {self.bsc_path}')

        working_cs = self.filter_cs(verbose=True)

        # Make it perform better by spawning a process function per
        # cs/file/funcs tuple, instead of spawning a thread per codestream
        args = []
        i = 1
        for cs, data in working_cs.items():
            # remove any previously generated files
            shutil.rmtree(self.get_cs_dir(cs, self.app), ignore_errors=True)

            # Apply patches before the LPs were created
            if self.apply_patches:
                self.apply_all_patches(cs, self.quilt_log)

            for fname, fdata in data['files'].items():
                args.append((i, fname, cs, fdata))
                i += 1

        self.total = len(args)
        logging.info(f'\nGenerating livepatches for {len(args)} file(s)...')
        logging.info('\t\tCodestream\tFile')

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = executor.map(self.process, args)
            for result in results:
                if result:
                    logging.error(f'{cs}: {result}')

        # Save the ext_symbols set by execute
        self.flush_cs_file()

        self.tem.refresh_codestreams(self.codestreams)

        self.group_equal_files(args)

        self.tem.generate_commit_msg_file()

        logging.info('Checking the externalized symbols in other architectures...')

        missing_syms = OrderedDict()

        # Iterate over each codestream, getting each file processed, and all
        # externalized symbols of this file
        # While we are at it, create the livepatches per codestream
        for cs, _ in working_cs.items():
            self.tem.GenerateLivePatches(cs)

            # Cleanup patches after the LPs were created
            if self.apply_patches:
                self.remove_patches(cs, self.quilt_log)

            # Map all symbols related to each obj, to make it check the output
            # of nm only once per object
            obj_syms = {}
            for f, fdata in self.get_cs_files(cs).items():
                for obj, syms in fdata['ext_symbols'].items():
                    obj_syms.setdefault(obj, [])
                    obj_syms[obj].extend(syms)

            for obj, syms in obj_syms.items():
                missing = self.check_symbol_archs(cs, obj, syms)
                if missing:
                    for arch, arch_syms in missing.items():
                        missing_syms.setdefault(arch, {})
                        missing_syms[arch].setdefault(obj, {})
                        missing_syms[arch][obj].setdefault(cs, [])
                        missing_syms[arch][obj][cs].extend(arch_syms)

            self.tem.CreateKbuildFile(cs)

        if missing_syms:
            with open(Path(self.bsc_path, 'missing_syms'), 'w') as f:
                f.write(json.dumps(missing_syms, indent=4))

            logging.warning('Symbols not found:')
            logging.warn(json.dumps(missing_syms, indent=4))

    def get_work_lp_file(self, cs, fname):
        return Path(self.get_work_dir(cs, fname, self.app), self.lp_out_file(fname))

    def get_cs_code(self, args):
        cs_files = {}

        # Mount the cs_files dict
        for arg in args:
            _, file, cs, _ = arg
            cs_files.setdefault(cs, [])

            fpath = self.get_work_lp_file(cs, file)
            with open(fpath, 'r+') as fi:
                src = fi.read()

                src = re.sub('#include \".+kconfig\.h\"', '', src)
                # Since 15.4 klp-ccp includes a compiler-version.h header
                src = re.sub('#include \".+compiler\-version\.h\"', '', src)
                # Since RT variants, there is now an definition for auto_type
                src = src.replace('#define __auto_type int\n', '')
                # We have problems with externalized symbols on macros. Ignore
                # codestream names specified on paths that are placed on the
                # expanded macros
                src = re.sub(f'{self.data}.+{file}', '', src)
                # We can have more details that can differ for long expanded
                # macros, like the patterns bellow
                src = re.sub(f'\.lineno = \d+,', '', src)

                # Remove any mentions to klpr_trace, since it's currently
                # buggy in klp-ccp
                src = re.sub('.+klpr_trace.+', '', src)

                # Remove clang-extract comments
                src = re.sub('clang-extract: .+', '', src)

                cs_files[cs].append((file , src ))

        return cs_files

    # cs_list should be only two entries
    def diff_cs(self, cs_list):
        args = []
        f1 = {}
        f2 = {}
        for cs in cs_list:
            for fname, _ in self.get_cs_files(cs).items():
                args.append((_, fname, cs, _))

        cs_code = self.get_cs_code(args)
        f1 = cs_code.get(cs_list[0])
        f2 = cs_code.get(cs_list[1])

        assert(len(f1) == len(f2))

        for i in range(len(f1)):
            content1 = f1[i][1].splitlines()
            content2 = f2[i][1].splitlines()

            for l in dl.unified_diff(content1, content2, fromfile=f1[i][0],
                                     tofile=f2[i][0]):
                print(l)

    # Get the code for each codestream, removing boilerplate code
    def group_equal_files(self, args):
        cs_equal = []
        processed = []

        cs_files = self.get_cs_code(args)
        toprocess = list(cs_files.keys())
        while len(toprocess):
            current_cs_list = []

            # Get an element, and check if it wasn't associated with a previous
            # codestream
            cs = toprocess.pop(0)
            if cs in processed:
                continue

            # last element, it's different from all other codestreams, so add it
            # to the cs_equal alone.
            if not toprocess:
                cs_equal.append([cs])
                break

            # start a new list with the current element to compare with others
            current_cs_list.append(cs)
            data_cs = cs_files[cs]
            len_data = len(data_cs)

            # Compare the file names, and file content between codestrams,
            # trying to find ones that have the same files and contents
            for cs_proc in toprocess:
                data_proc = cs_files[cs_proc]

                if len_data != len(data_proc):
                    continue

                ok = True
                for i in range(len_data):
                    file, src = data_cs[i]
                    file_proc, src_proc = data_proc[i]

                    if file != file_proc or src != src_proc:
                        ok = False
                        break

                # cs is equal to cs_proc, with the same number of files, same
                # file names, and the files have the same content. So we don't
                # need to process cs_proc later in the process
                if ok:
                    processed.append(cs_proc)
                    current_cs_list.append(cs_proc)

            # Append the current list of equal codestreams to a global list to
            # be grouped later
            cs_equal.append(natsorted(current_cs_list))

        # cs_equal will contain a list of lists with codestreams that share the
        # same code
        groups = []
        for cs_list in cs_equal:
            groups.append(' '.join(lp_utils.classify_codestreams(cs_list)))

        with open(Path(self.bsc_path, self.app, 'groups'), 'w') as f:
            f.write('\n'.join(groups))

        logging.info('\nGrouping codestreams that share the same content and files:')
        for group in groups:
            logging.info(f'\t{group}')