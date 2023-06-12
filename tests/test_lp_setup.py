import json
import logging
from pathlib import Path
import os
import shutil
import sys
import unittest
from unittest.mock import patch

from tests import utils

sys.path.append('..')
from lp_setup import Setup

class LpSetupTest(unittest.TestCase):
    def setUp(self):
        # Avoid searching for patches kernels
        os.environ['KLP_KERNEL_SOURCE'] = ''

        logging.disable(logging.INFO)

    def setup(self, dargs, init = False):
        with self.assertNoLogs(level='WARNING') as anl:
            return utils.setup(dargs, init)

    def setup_assert_logs(self, dargs, alevel, msg):
        with self.assertLogs(level=alevel) as logs:
            utils.setup(dargs, True)

        self.assertRegex(logs.output[0], msg)

    def setup_and_assert(self, dargs, exc, msg = None):
        with self.assertRaises(exc) as ar:
            utils.setup(dargs, True)

        if not msg:
            self.assertEqual(str(ar.exception), msg)

    def test_missing_conf_archs(self):
        v = utils.sargs()
        v['archs'] = []
        self.setup_and_assert(v, ValueError,
                'Please specify --conf when not all architectures are supported')

        # All archs supported, complain about file-funcs
        v['archs'] = ['x86_64', 'ppc64le', 's390x']
        self.setup_and_assert(v, ValueError,
                'You need to specify at least one of the file-funcs variants!')

        # Only one arch supported, but conf informed, complain about file-funcs
        v['archs'] = ['x86_64']
        v['conf'] = 'CONFIG_TUN'
        self.setup_and_assert(v, ValueError,
                'You need to specify at least one of the file-funcs variants!')

    def test_missing_conf_mod(self):
        v = utils.sargs()
        v['module'] = 'tun'
        self.setup_and_assert(v, ValueError,
                'Please specify --conf when a module is specified')

        # if module is vmlinux it should only complains about file-funcs
        v['module'] = 'vmlinux'
        self.setup_and_assert(v, ValueError,
                'You need to specify at least one of the file-funcs variants!')

    def test_missing_file_funcs(self):
        v = utils.sargs()
        v['module'] = 'tun'
        v['conf'] = 'CONFIG_TUN'
        self.setup_and_assert(v, ValueError,
                'You need to specify at least one of the file-funcs variants!')

    def test_file_funcs_ok(self):
        v = utils.sargs()
        v['module'] = 'tun'
        v['conf'] = 'CONFIG_TUN'
        v['file_funcs'] = ['drivers/net/tun.c', 'tun_chr_ioctl', 'tun_free_netdev']
        self.setup(v)

        # Checks if the variants of file-funcs also work
        v['file_funcs'] = []
        v['mod_file_funcs'] = [['tun', 'drivers/net/tun.c', 'tun_chr_ioctl',
                                'tun_free_netdev']]
        self.setup(v)

        v['mod_file_funcs'] = []
        v['conf_mod_file_funcs'] = [['CONFIG_TUN', 'tun', 'drivers/net/tun.c',
                                     'tun_chr_ioctl', 'tun_free_netdev']]
        self.setup(v)

    def test_non_existent_file(self):
        v = utils.sargs()
        v['module'] = 'tun'
        v['conf'] = 'CONFIG_TUN'
        v['file_funcs'] = [['drivers/net/tuna.c', 'tun_chr_ioctl',
                            'tun_free_netdev']]

        self.setup_and_assert(v, RuntimeError, 'File drivers/net/tuna.c not found')

    def test_existent_file(self):
        v = utils.sargs()
        v['module'] = 'tun'
        v['conf'] = 'CONFIG_TUN'
        v['file_funcs'] = [['drivers/net/tun.c', 'tun_chr_ioctl',
                            'tun_free_netdev']]
        self.setup(v, True)

    def test_invalid_sym(self):
        v = utils.sargs()
        v['module'] = 'tun'
        v['conf'] = 'CONFIG_TUN'
        v['file_funcs'] = [['drivers/net/tun.c', 'tun_chr_ioctll',
                            'tun_free_netdev']]

        self.setup_assert_logs(v, 'WARNING',
                'Symbols tun_chr_ioctll not found on tun')

    def test_non_existent_module(self):
        v = utils.sargs()
        v['module'] = 'tuna'
        v['conf'] = 'CONFIG_TUN'
        v['file_funcs'] = [['drivers/net/tun.c', 'tun_chr_ioctl',
                            'tun_free_netdev']]

        self.setup_and_assert(v, RuntimeError, 'Module not found: tuna')

    def test_check_symbol_addr_s390(self):
        v = utils.sargs()
        v['filter'] = '12.4u35'
        v['module'] = 'sch_qfq'
        v['conf'] = 'CONFIG_NET_SCH_QFQ'
        v['file_funcs'] = [['net/sched/sch_qfq.c', 'qfq_change_class']]
        s = self.setup(v, True)

        # The address of qfq_policy on s390x ends with a character, a bug that
        # was fixed by checking for \w instead of \d.
        # With the fix in place, check_symbol_archs should return an empty list
        self.assertFalse(s.check_symbol_archs('12.4u35', 'sch_qfq',
                                              ['qfq_policy']))

    def test_check_conf_mod_file_funcs(self):
        v = utils.sargs()
        cs = '15.4u12'
        v['archs'] = ['x86_64']
        v['filter'] = cs
        v['module'] = 'sch_qfq'
        v['conf'] = 'CONFIG_NET_SCH_QFQ'
        v['file_funcs'] = [['net/sched/sch_qfq.c', 'qfq_change_class']]
        v['mod_file_funcs'] = [[ 'btsdio',
                                 'drivers/bluetooth/btsdio.c',
                                 'btsdio_probe', 'btsdio_remove' ]]

        # CONF should be same, but mod doesn't
        self.setup(v, True)

        with open(Path(utils.basedir(v), 'codestreams.json')) as f:
            data = json.loads(f.read())[cs]['files']

        sch = data['net/sched/sch_qfq.c']
        bts = data['drivers/bluetooth/btsdio.c']
        self.assertEqual(sch['conf'], bts['conf'])
        self.assertEqual(sch['module'], 'sch_qfq')
        self.assertEqual(bts['module'], 'btsdio')

        v['conf_mod_file_funcs'] = [[ 'CONFIG_BT_HCIBTSDIO',
                                     'btsdio',
                                     'drivers/bluetooth/btsdio.c',
                                     'btsdio_probe', 'btsdio_remove' ]]

        # Now, conf and module should be different
        s = self.setup(v, True)

        with open(Path(utils.basedir(v), 'codestreams.json')) as f:
            data = json.loads(f.read())[cs]['files']

        sch = data['net/sched/sch_qfq.c']
        bts = data['drivers/bluetooth/btsdio.c']
        self.assertEqual(sch['conf'], 'CONFIG_NET_SCH_QFQ')
        self.assertEqual(sch['module'], 'sch_qfq')
        self.assertEqual(bts['conf'], 'CONFIG_BT_HCIBTSDIO')
        self.assertEqual(bts['module'], 'btsdio')

if __name__ == '__main__':
    unittest.main()