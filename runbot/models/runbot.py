import time
import logging
import glob
import random
import re
import subprocess
from datetime import timedelta

from ..common import fqdn, dt2time, dest_reg, os
from ..container import docker_ps, docker_stop

from odoo import models, fields
from odoo.osv import expression
from odoo.tools import config
from odoo.modules.module import get_module_resource

_logger = logging.getLogger(__name__)

 # after this point, not realy a repo buisness
class Runbot(models.AbstractModel):
    _name = 'runbot.runbot'
    _description = 'Base runbot model'

    def _commit(self):
        self.env.cr.commit()
        self.env.cache.invalidate()
        self.env.clear()

    def _root(self):
        """Return root directory of repository"""
        default = os.path.join(os.path.dirname(__file__), '../static')
        return os.path.abspath(default)

    def _scheduler(self, host):
        nb_workers = host.get_nb_worker()

        self._gc_testing(host)
        self._commit()
        for build in self._get_builds_with_requested_actions(host):
            build._process_requested_actions()
            self._commit()
        for build in self._get_builds_to_schedule(host):
            build._schedule()
            self._commit()
        self._assign_pending_builds(host, nb_workers, [('build_type', '!=', 'scheduled')])
        self._commit()
        self._assign_pending_builds(host, nb_workers-1 or nb_workers)
        self._commit()
        for build in self._get_builds_to_init(host):
            build._init_pendings(host)
            self._commit()
        self._gc_running(host)
        self._commit()
        self._reload_nginx()

    def build_domain_host(self, host, domain=None):
        domain = domain or []
        return [('host', '=', host.name)] + domain

    def _get_builds_with_requested_actions(self, host):
        return self.env['runbot.build'].search(self.build_domain_host(host, [('requested_action', 'in', ['wake_up', 'deathrow'])]))

    def _get_builds_to_schedule(self, host):
        return self.env['runbot.build'].search(self.build_domain_host(host, [('local_state', 'in', ['testing', 'running'])]))

    def _assign_pending_builds(self, host, nb_workers, domain=None):
        if host.assigned_only or nb_workers <= 0:
            return
        domain_host = self.build_domain_host(host)
        reserved_slots = self.env['runbot.build'].search_count(domain_host + [('local_state', 'in', ('testing', 'pending'))])
        assignable_slots = (nb_workers - reserved_slots)
        if assignable_slots > 0:
            allocated = self._allocate_builds(host, assignable_slots, domain)
            if allocated:
                _logger.debug('Builds %s where allocated to runbot' % allocated)

    def _get_builds_to_init(self, host):
        domain_host = self.build_domain_host(host)
        used_slots = self.env['runbot.build'].search_count(domain_host + [('local_state', '=', 'testing')])
        available_slots = host.get_nb_worker() - used_slots
        if available_slots <= 0:
            return self.env['runbot.build']
        return self.env['runbot.build'].search(domain_host + [('local_state', '=', 'pending')], limit=available_slots)

    def _gc_running(self, host):
        running_max = host.get_running_max()
        # terminate and reap doomed build
        domain_host = self.build_domain_host(host)
        Build = self.env['runbot.build']
        # some builds are marked as keep running
        cannot_be_killed_ids = Build.search(domain_host + [('keep_running', '!=', True)]).ids
        # we want to keep one build running per sticky, no mather which host
        sticky_branches_ids = self.env['runbot.branch'].search([('sticky', '=', True)]).ids
        # search builds on host on sticky branches, order by position in branch history
        if sticky_branches_ids:
            self.env.cr.execute("""
                SELECT
                    id
                FROM (
                    SELECT
                        bu.id AS id,
                        bu.host as host,
                        row_number() OVER (PARTITION BY branch_id order by bu.id desc) AS row
                    FROM
                        runbot_branch br INNER JOIN runbot_build bu ON br.id=bu.branch_id
                    WHERE
                        br.id in %s AND (bu.hidden = 'f' OR bu.hidden IS NULL)
                    ) AS br_bu
                WHERE
                    row <= 4 AND host = %s
                ORDER BY row, id desc
                """, [tuple(sticky_branches_ids), host.name]
            )
            cannot_be_killed_ids += self.env.cr.fetchall() #looks wrong?
        cannot_be_killed_ids = cannot_be_killed_ids[:running_max]  # ensure that we don't try to keep more than we can handle

        build_ids = Build.search(domain_host + [('local_state', '=', 'running'), ('id', 'not in', cannot_be_killed_ids)], order='job_start desc').ids
        Build.browse(build_ids)[running_max:]._kill()

    def _gc_testing(self, host):
        """garbage collect builds that could be killed"""
        # decide if we need room
        Build = self.env['runbot.build']
        domain_host = self.build_domain_host(host)
        testing_builds = Build.search(domain_host + [('local_state', 'in', ['testing', 'pending']), ('requested_action', '!=', 'deathrow')])
        used_slots = len(testing_builds)
        available_slots = host.get_nb_worker() - used_slots
        nb_pending = Build.search_count([('local_state', '=', 'pending'), ('host', '=', False)])
        if available_slots > 0 or nb_pending == 0:
            return
        self.env.cr.execute("""
            SELECT
                runbot_build.id
            FROM
                runbot_build
            LEFT JOIN
                runbot_batch_slot ON runbot_batch_slot.build_id = runbot_build.id
            LEFT JOIN
                runbot_batch ON runbot_batch_slot.batch_id = runbot_batch.id
            LEFT JOIN
                runbot_bundle ON runbot_bundle.id = runbot_batch.bundle_id
            WHERE
                runbot_build.local_state in ('testing', 'pending')
            AND
                runbot_bundle.sticky = true;
            """)
        cannot_be_killed_ids = self.env.cr.fetchall()
        for build in testing_builds:
            top_parent = build._get_top_parent()
            if build.id not in cannot_be_killed_ids and top_parent.id not in cannot_be_killed_ids:
                newer_candidates = Build.search([
                    ('id', '>', build.id),
                    ('params_id', '=', top_parent.params_id.id),
                    ('build_type', '=', 'normal'),
                    ('parent_id', '=', False),
                    ('hidden', '=', False),
                ])
                if newer_candidates:
                    top_parent._ask_kill(message='Build automatically killed, newer build found %s.' % newer_candidates.ids)

    def _allocate_builds(self, host, nb_slots, domain=None):
        if nb_slots <= 0:
            return []
        non_allocated_domain = [('local_state', '=', 'pending'), ('host', '=', False)]
        if domain:
            non_allocated_domain = expression.AND([non_allocated_domain, domain])
        e = expression.expression(non_allocated_domain, self.env['runbot.build'])
        assert e.get_tables() == ['"runbot_build"']
        where_clause, where_params = e.to_sql()

        # self-assign to be sure that another runbot batch cannot self assign the same builds

        # TODO check: priority removed. Not a big deal, but maybe for staging. Add a priority on bundle, + transfer priority to build?
        query = """UPDATE
                        runbot_build
                    SET
                        host = %%s
                    WHERE
                        runbot_build.id IN (
                            SELECT runbot_build.id
                            FROM runbot_build
                            WHERE
                                %s
                            ORDER BY
                                array_position(array['normal','rebuild','indirect','scheduled']::varchar[], runbot_build.build_type) ASC
                            FOR UPDATE OF runbot_build SKIP LOCKED
                            LIMIT %%s
                        )
                    RETURNING id""" % where_clause
        self.env.cr.execute(query, [host.name] + where_params + [nb_slots])
        return self.env.cr.fetchall()

    def _domain(self):
        return self.env.get('ir.config_parameter').get_param('runbot.runbot_domain', fqdn())

    def _reload_nginx(self):
        # TODO move this elsewhere
        env = self.env
        settings = {}
        settings['port'] = config.get('http_port')
        settings['runbot_static'] = os.path.join(get_module_resource('runbot', 'static'), '')
        nginx_dir = os.path.join(self._root(), 'nginx')
        settings['nginx_dir'] = nginx_dir
        settings['re_escape'] = re.escape
        settings['fqdn'] = fqdn()

        icp = env['ir.config_parameter'].sudo()
        nginx = icp.get_param('runbot.runbot_nginx', True)  # or just force nginx?

        if nginx:
            settings['builds'] = env['runbot.build'].search([('local_state', '=', 'running'), ('host', '=', fqdn())])

            nginx_config = env['ir.ui.view'].render_template("runbot.nginx_config", settings)
            os.makedirs(nginx_dir, exist_ok=True)
            content = None
            nginx_conf_path = os.path.join(nginx_dir, 'nginx.conf')
            content = ''
            if os.path.isfile(nginx_conf_path):
                with open(nginx_conf_path, 'rb') as f:
                    content = f.read()
            if content != nginx_config:
                _logger.debug('reload nginx')
                with open(nginx_conf_path, 'wb') as f:
                    f.write(nginx_config)
                try:
                    pid = int(open(os.path.join(nginx_dir, 'nginx.pid')).read().strip(' \n'))
                    os.kill(pid, signal.SIGHUP)
                except Exception:
                    _logger.debug('start nginx')
                    if subprocess.call(['/usr/sbin/nginx', '-p', nginx_dir, '-c', 'nginx.conf']):
                        # obscure nginx bug leaving orphan worker listening on nginx port
                        if not subprocess.call(['pkill', '-f', '-P1', 'nginx: worker']):
                            _logger.debug('failed to start nginx - orphan worker killed, retrying')
                            subprocess.call(['/usr/sbin/nginx', '-p', nginx_dir, '-c', 'nginx.conf'])
                        else:
                            _logger.debug('failed to start nginx - failed to kill orphan worker - oh well')

    def _get_cron_period(self):
        """ Compute a randomized cron period with a 2 min margin below
        real cron timeout from config.
        """
        cron_limit = config.get('limit_time_real_cron')
        req_limit = config.get('limit_time_real')
        cron_timeout = cron_limit if cron_limit > -1 else req_limit
        return cron_timeout / 2

    def _cron(self):
        """
        This method is the default cron for new commit discovery and build sheduling.
        The cron runs for a long time to avoid spamming logs
        """
        start_time = time.time()
        timeout = self._get_cron_period()
        get_param = self.env['ir.config_parameter'].get_param
        update_frequency = int(get_param('runbot.runbot_update_frequency', default=10))
        runbot_do_fetch = get_param('runbot.runbot_do_fetch')
        runbot_do_schedule = get_param('runbot.runbot_do_schedule')
        if not runbot_do_fetch and not runbot_do_schedule:
            timeout = min(timeout, 10)
        host = self.env['runbot.host']._get_current()
        host.set_psql_conn_count()
        host.last_start_loop = fields.Datetime.now()
        self._commit()
        # Bootstrap
        host._bootstrap()
        if runbot_do_schedule:
            host._docker_build()
            self._source_cleanup()
            self.env['runbot.build']._local_cleanup()
            self._docker_cleanup()

        while time.time() - start_time < timeout:
            if runbot_do_fetch:
                repos = self.env['runbot.repo'].search([('mode', '!=', 'disabled')])
                repos = repos.filtered(lambda rep: rep.remote_ids)
                repos._update(force=False)
                repos._create_batches()
                self._commit()
                self.env['runbot.batch']._process()
                self._commit()
            if runbot_do_schedule:
                while time.time() - start_time < update_frequency:
                    time.sleep(self._scheduler_loop_turn(host, update_frequency))
            self._commit()

        host.last_end_loop = fields.Datetime.now()


    def _scheduler_loop_turn(self, host, default_sleep=1):
        try:
            self._scheduler(host)
            host.last_success = fields.Datetime.now()
            self._commit()
        except Exception as e:
            self.env.cr.rollback()
            self.env.clear()
            _logger.exception(e)
            message = str(e)
            if host.last_exception == message:
                host.exception_count += 1
            else:
                host.last_exception = str(e)
                host.exception_count = 1
            self._commit()
            return random.uniform(0, 3)
        else:
            if host.last_exception:
                host.last_exception = ""
                host.exception_count = 0
            return default_sleep

    def _source_cleanup(self):
        try:
            if self.pool._init:
                return
            _logger.info('Source cleaning')
            # we can remove a source only if no build are using them as name or rependency_ids aka as commit
            cannot_be_deleted_builds = self.env['runbot.build'].search([('host', '=', fqdn()), ('local_state', 'not in', ('done', 'duplicate'))])
            cannot_be_deleted_path = set()
            for build in cannot_be_deleted_builds:
                for commit in build.build_commit_ids:
                    cannot_be_deleted_path.add(commit._source_path())

            to_delete = set()
            to_keep = set()
            repos = self.env['runbot.repo'].search([('mode', '!=', 'disabled')])
            for repo in repos:
                repo_source = os.path.join(self._root(), 'sources', repo.name, '*') # TODO check source folder
                for source_dir in glob.glob(repo_source):
                    if source_dir not in cannot_be_deleted_path:
                        to_delete.add(source_dir)
                    else:
                        to_keep.add(source_dir)

            # we are comparing cannot_be_deleted_path with to keep to sensure that the algorithm is working, we want to avoid to erase file by mistake
            # note: it is possible that a parent_build is in testing without checkouting sources, but it should be exceptions
            if to_delete:
                if cannot_be_deleted_path != to_keep:
                    _logger.warning('Inconsistency between sources and database: \n%s \n%s' % (cannot_be_deleted_path-to_keep, to_keep-cannot_be_deleted_path))
                to_delete = list(to_delete)
                to_keep = list(to_keep)
                cannot_be_deleted_path = list(cannot_be_deleted_path)
                for source_dir in to_delete:
                    _logger.info('Deleting source: %s' % source_dir)
                    assert 'static' in source_dir
                    shutil.rmtree(source_dir)
                _logger.info('%s/%s source folder where deleted (%s kept)' % (len(to_delete), len(to_delete+to_keep), len(to_keep)))
        except:
            _logger.exception('An exception occured while cleaning sources')
            pass

    def _docker_cleanup(self):
        _logger.info('Docker cleaning')
        docker_ps_result = docker_ps()
        containers = {int(dc.split('-', 1)[0]):dc for dc in docker_ps_result if dest_reg.match(dc)}
        if containers:
            candidates = self.env['runbot.build'].search([('id', 'in', list(containers.keys())), ('local_state', '=', 'done')])
            for c in candidates:
                _logger.info('container %s found running with build state done', containers[c.id])
                docker_stop(containers[c.id], c._path())
        ignored = {dc for dc in docker_ps_result if not dest_reg.match(dc)}
        if ignored:
            _logger.debug('docker (%s) not deleted because not dest format', " ".join(list(ignored)))