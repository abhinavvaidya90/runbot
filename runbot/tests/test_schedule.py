# -*- coding: utf-8 -*-
import datetime
from unittest.mock import patch
from odoo.tests import common
import odoo
from .common import RunbotCase


class TestSchedule(RunbotCase):


    @patch('odoo.addons.runbot.models.build.os.path.getmtime')
    @patch('odoo.addons.runbot.models.build.docker_state')
    def test_schedule_mark_done(self, mock_docker_state, mock_getmtime):
        """ Test that results are set even when job_30_run is skipped """
        job_end_time = datetime.datetime.now()
        mock_getmtime.return_value = job_end_time.timestamp()

        params = self.BuildParameters.create({
            'version_id': self.version_master,
            'project_id': self.project,
            'config_id': self.env.ref('runbot.runbot_build_config_default').id,
        })
        build = self.Build.create({
            'local_state': 'testing',
            'port': '1234',
            'host': 'runbotxx',
            'job_start': datetime.datetime.now(),
            'active_step': self.env.ref('runbot.runbot_build_config_step_run').id,
            'params_id': params.id
        })
        mock_docker_state.return_value = 'UNKNOWN'
        self.assertEqual(build.local_state, 'testing')
        build._schedule()  # too fast, docker not started
        self.assertEqual(build.local_state, 'testing')

        build.write({'job_start': datetime.datetime.now() - datetime.timedelta(seconds=70)})  # docker never started
        build._schedule()
        self.assertEqual(build.local_state, 'done')
        self.assertEqual(build.local_result, 'ok')
