from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase

from mainApp.tests.test_schoolpay import SchoolPayTestBase


class SyncCommandTests(SchoolPayTestBase):
    def test_command_syncs_range(self):
        with mock.patch("mainApp.schoolpay.service.sync_school", return_value={}) as sync:
            call_command("schoolpay_sync", "--from", "2026-02-01", "--to", "2026-05-01", stdout=StringIO())
        args = sync.call_args.args
        self.assertEqual((str(args[1]), str(args[2])), ("2026-02-01", "2026-05-01"))


class SyncCommandEmptyTests(TestCase):
    def test_no_schools(self):
        with self.assertRaises(CommandError):
            call_command("schoolpay_sync", stdout=StringIO())
