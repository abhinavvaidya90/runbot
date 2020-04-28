import glob
import re
import time

from ..common import s2human, dt2time
from babel.dates import format_timedelta
from datetime import timedelta

from collections import defaultdict

from odoo import models, fields, api



#Todo test: create will invalid branch name, pull request


class Version(models.Model):
    _name = "runbot.version"
    _description = "Version"

    name = fields.Char('Version name')
    number = fields.Char('Comparable version number', compute='_compute_version_number', store=True)

    @api.depends('name')
    def _compute_version_number(self):
        for version in self:
            if version.name == 'master':
                version.number = '~'
            else:
                # max version number with this format: 99.99
                version.number = '.'.join([elem.zfill(2) for elem in re.sub(r'[^0-9\.]', '', version.name).split('.')])

class ProjectCategory(models.Model):
    _name = 'runbot.project.category'
    _description = 'Category'

    name = fields.Char('Category name', required=True, unique=True, help="Name of the base branch")
    trigger_ids = fields.One2many('runbot.trigger', 'category_id', string='Triggers', required=True, unique=True, help="Name of the base branch")

class Project(models.Model):
    _name = "runbot.project"
    _description = "Project"

    name = fields.Char('Project name', required=True, unique=True, help="Name of the base branch")
    category_id = fields.Many2one('runbot.project.category')
    sticky = fields.Boolean(store=True)
    is_base = fields.Boolean('Is base')
    version_id = fields.Many2one('runbot.version', 'Version')
    version_number = fields.Char(related='version_id.number', store=True)
    branch_ids = fields.One2many('runbot.branch', 'project_id')

    # custom behaviour
    rebuild_requested = fields.Boolean("Request a rebuild", help="Rebuild the latest commit even when no_auto_build is set.", default=False)
    no_build = fields.Boolean('No build')
    modules = fields.Char("Modules to install", help="Comma-separated list of modules to install and test.")

    batch_ids = fields.One2many('runbot.batch', 'project_id')
    last_batch = fields.Many2one('runbot.batch', index=True)
    last_batchs = fields.Many2many('runbot.batch', 'Last instances', compute='_compute_last_batchs')

    def _init_column(self, column_name):
        if column_name not in ('version_number',):
            return super()._init_column(column_name)

        if column_name == 'version_number':
            import traceback
            traceback.print_stack()
            for version in self.env['runbot.version'].search([]):
                self.search([('version_id', '=', version.id)]).write({'version_number':version.number})


    def _compute_last_batchs(self):
        if self:
            batch_ids = defaultdict(list)
            self.env.cr.execute("""
                SELECT
                    id
                FROM (
                    SELECT
                        instance.id AS id,
                        row_number() OVER (PARTITION BY instance.project_id order by instance.id desc) AS row
                    FROM
                        runbot_project project INNER JOIN runbot_batch instance ON project.id=instance.project_id
                    WHERE
                        project.id in %s
                    ) AS project_batch
                WHERE
                    row <= 4
                ORDER BY row, id desc
                """, [tuple(self.ids)]
            )
            instances = self.env['runbot.batch'].browse([r[0] for r in self.env.cr.fetchall()])
            for instance in instances:
                batch_ids[instance.project_id.id].append(instance.id)

            for project in self:
                project.last_batchs = [(6, 0, batch_ids[project.id])]


    def toggle_request_project_rebuild(self):
        for branch in self:
            if not branch.rebuild_requested:
                branch.rebuild_requested = True
                branch.repo_id.sudo().set_hook_time(time.time())
            else:
                branch.rebuild_requested = False

    # version can change in case of retarget or manual operation from user


    #base_id = fields.Many2one('runbot.project', 'Base project', compute='_compute_closest_base' 
    #    help='A corresponding project that is a base, ususally a target, (master, or other version)')
    #forced_base_id = fields.Many2one('runbot.project', 'Forced base project')

    def write(self, values):
        super().write(values)
        if 'is_base' in values:
            for project in self:
                self.env['runbot.project'].search([('name', '=like', '%s%%' % project.name)])._compute_closest_base()

    def _get(self, name, category_id):
        project = self.search([('name', '=', name), ('category_id', '=', category_id)])
        if not project:
            self.create({
                'name': name,
                'category_id': category_id,
            })
        return project

    @api.depends('is_base', 'forced_base', 'base_id.is_base')
    def _compute_closest_base(self):
        bases_by_category = {}
        for project in self:
            if self.is_base:
                return self
            category_id = project.category_id
            if category_id in bases_by_category:  # small perf imp for udge bartched
                base_projects = bases_by_category[category_id]
            else:
                base_projects = self.search([('is_base', '=', True), ('category_id', '=', category_id)])
                bases_by_category[category_id] = base_projects
            for candidate in base_projects:
                if project.name.startswith(candidate.name):
                    project.base_id = candidate
                    break
                elif project.name == 'master':
                    project.base_id = candidate


    def _get_preparing_batch(self):
        # find last project instance or create one
        if self.last_batch.state != preparing:
            preparing = self.env['runbot.batch'].create({
                'last_update': fields.Datetime.Now(),
                'project_id': self,
                'state': 'creating',
            })
            self.last_batch = preparing
        return self.last_batch

    def _target_changed(self):
        self.add_warning()

    def _last_succes(self):
        # search last project where all linked builds are success
        return None


class ProjectInstance(models.Model):
    _name = "runbot.batch"
    _description = "Project instance"

    last_update = fields.Datetime('Last ref update')
    project_id = fields.Many2one('runbot.project', required=True, index=True)
    project_commit_ids = fields.One2many('runbot.batch.commit', 'batch_id')
    slot_ids = fields.One2many('runbot.batch.slot', 'batch_id')
    state = fields.Selection([('preparing', 'Preparing'), ('ready', 'Ready'), ('complete', 'Complete'), ('done', 'Done')])
    hidden = fields.Boolean('Hidden', default=False)
    age = fields.Integer(compute='_compute_age', string='Build age')


    @api.depends('create_date')
    def _compute_age(self):
        """Return the time between job start and now"""
        for instance in self:
            if instance.create_date:
                instance.age = int(1587461700 - dt2time(instance.create_date)) # TODO remove hack time.time()
            else:
                instance.buildage_age = 0

    def get_formated_age(self):
        return format_timedelta(
            timedelta(seconds=-self.age),
            threshold=2.1,
            add_direction=True, locale='en'
        )
    def _add_commit(self, commit):
        # if not the same hash for repo_group:
        self.last_update = fields.Datetime.now()
        for project_commit in self.project_commit_ids:
            # case 1: a commit already exists for the repo (pr+branch, or fast push)
            if project_commit.commit_id.repo_group_id == commit.repo_group_id:
                project_commit.commit_id = commit
                break
        else:
            self.env['runbot.batch.commit'].create({
                'commit_id': commit.id,
                'batch_id': self.id,
                'match_type': 'head'
            })

    def _start(self):
        #  For all commit on real branches:
        self.state = 'ready'
        triggers = self.env['runbot.trigger'].search([('category_id', '=', self.project_id.category_id)])
        pushed_repo_groups = self.project_commit_ids.mapped('repos_group_ids') | self.project_commit_ids.mapped('dependency_ids')

        #  save commit state for all trigger dependencies and repo
        trigger_repos_groups = triggers.mapped('repo_group_id')
        for missing_repo_group in pushed_repo_groups-trigger_repos_groups:
            break
            # find commit for missing_repo_group in a corresponding branch: branch head in the same project, or fallback on base_repo
        for trigger in triggers:
            if trigger.repo_group_ids & pushed_repo_groups:  # there is a new commit in this in this trigger
                break
                # todo create build

    def github_status(self):
        pass

            # todo execute triggers


class ProjectInstanceCommit(models.Model):
    _name = 'runbot.batch.commit'
    _description = "Project instance commit"

    commit_id = fields.Many2one('runbot.commit', index=True)
    repo_id = fields.Many2one('runbot.repo', string='Repo') # discovered in repo
    # ??? base_commit_id = fields.Many2one('runbot.commit')
    batch_id = fields.Many2one('runbot.batch', index=True)
    match_type = fields.Selection([('new', 'New head of branch'), ('head', 'Head of branch'), ('default', 'Found on base branch')])  # HEAD, DEFAULT


class ProjectInstanceSlot(models.Model):
    _name = 'runbot.batch.slot'
    _description = 'Link between a project instance and a build'

    _fa_link_type = {'created': 'hashtag', 'matched': 'link', 'rebuild': 'refresh'}

    batch_id = fields.Many2one('runbot.batch')
    trigger_id = fields.Many2one('runbot.trigger', index=True)
    build_id = fields.Many2one('runbot.build', index=True)
    link_type = fields.Selection([('created', 'Build created'), ('matched', 'Existing build matched'), ('rebuild', 'Rebuild')], required=True) # rebuild type?
    active = fields.Boolean('Attached')
    result = fields.Selection("Result", related='build_id.global_result')
    # rebuild, what to do: since build ccan be in multiple instance:
    # - replace for all instance?
    # - only available on instance and replace for instance only?
    # - create a new project instance will new linked build?

    def fa_link_type(self):
        return self._fa_link_type.get(self.link_type, 'exclamation-triangle')