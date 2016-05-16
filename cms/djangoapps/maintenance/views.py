"""
Views for the maintenance app.
"""
import logging
from django.db import transaction
from django.core.urlresolvers import reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.translation import ugettext_lazy as _
from django.views.generic import View

from edxmako.shortcuts import render_to_response
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey
from xmodule.modulestore import ModuleStoreEnum
from xmodule.modulestore.django import modulestore
from xmodule.modulestore.exceptions import ItemNotFoundError

from contentstore.management.commands.utils import get_course_versions
from util.views import require_global_staff


log = logging.getLogger(__name__)

MAINTENANCE_COMMANDS = {
    "force_publish_course": {
        "url": reverse_lazy("maintenance:force_publish_course"),
        "name": _("Force Publish Course"),
        "slug": "force_publish_course",
        "description": _("Force publish course."),
    },
}


COURSE_KEY_ERROR_MESSAGES = {
    'empty_course_key': _('Please provide course id.'),
    'invalid_course_key': _('Invalid course key.'),
    'course_key_not_found': _('No matching course found.')
}


def get_maintenace_urls():
    """
    Returns all URLs for maintenance app.
    """
    url_list = []
    for key, val in MAINTENANCE_COMMANDS.items():  # pylint: disable=unused-variable
        url_list.append(val['url'])
    return url_list


class MaintenanceIndexView(View):
    """
    Index view for maintenance dashboard, used by the escalation team.

    This view lists some commands/tasks that can be used to dry run or execute directly.
    """

    @method_decorator(require_global_staff)
    def get(self, request):
        """Render the maintenance index view. """
        return render_to_response('maintenance/index.html', {
            "commands": MAINTENANCE_COMMANDS,
        })


class MaintenanceBaseView(View):
    """
    Base class for Maintenance views.
    """

    template = 'maintenance/container.html'

    def __init__(self, command=None):
        self.context = {
            'command': command if command else '',
            'form_data': {},
            'error': False,
            'msg': ''
        }

    def render_response(self):
        """ A short method to render_to_response that renders response."""
        return render_to_response(self.template, self.context)

    @method_decorator(require_global_staff)
    def get(self, request):
        """Render get view."""
        return self.render_response()

    def validate_course_key(self, course_key, branch=ModuleStoreEnum.BranchName.draft):
        """
        Validates the course_key that would be used by maintenance app views.

        Arguments:
            course_key (string): a course key
            branch: a course locator branch, default value is ModuleStoreEnum.BranchName.draft .
                    values can be either ModuleStoreEnum.BranchName.draft or ModuleStoreEnum.BranchName.published.

        Returns:
            course_usage_key (CourseLocator): course usage locator
        """
        if not course_key:
            raise Exception(COURSE_KEY_ERROR_MESSAGES['empty_course_key'])

        course_usage_key = CourseKey.from_string(course_key)

        if not modulestore().has_course(course_usage_key):
            raise ItemNotFoundError(COURSE_KEY_ERROR_MESSAGES['course_key_not_found'])

        # get branch specific locator
        course_usage_key = course_usage_key.for_branch(branch)

        return course_usage_key


class ForcePublishCourseView(MaintenanceBaseView):
    """
    View for force publishing state of the course, used by the escalation team.
    """

    def __init__(self):
        super(ForcePublishCourseView, self).__init__(MAINTENANCE_COMMANDS['force_publish_course'])
        self.context.update({
            'current_versions': [],
            'updated_versions': [],
            'form_data': {
                'course_id': '',
                'is_dry_run': True
            }
        })

    @transaction.atomic
    @method_decorator(require_global_staff)
    def post(self, request):
        """
        Force publishes a course.

        Arguments:
            course_id (string): a request parameter containing course id
            is_dry_run (string): a request parameter containing dry run value.
                                 It is obtained from checkbox so it has either values 'on' or ''.
        """

        course_id = request.POST.get('course-id')
        is_dry_run = bool(request.POST.get('dry-run'))

        self.context.update({
            'form_data': {
                'course_id': course_id,
                'is_dry_run': is_dry_run
            }
        })

        try:
            course_usage_key = self.validate_course_key(course_id)
        except InvalidKeyError:
            self.context['error'] = True
            self.context['msg'] = COURSE_KEY_ERROR_MESSAGES['invalid_course_key']
        except ItemNotFoundError as e:
            self.context['error'] = True
            self.context['msg'] = e.message
        except Exception as e:
            self.context['error'] = True
            self.context['msg'] = e.message

        if self.context['error']:
            return self.render_response()

        source_store = modulestore()._get_modulestore_for_courselike(course_usage_key)  # pylint: disable=protected-access
        if not hasattr(source_store, 'force_publish_course'):
            self.context['msg'] = _("Force publish course does not support old mongo style courses.")
            logging.info(
                "Force publish course does not support old mongo style courses. \
                %s attempted to force publish the course %s.",
                request.user,
                course_id,
                exc_info=True
            )
            return self.render_response()

        current_versions = get_course_versions(course_id)

        # if publish and draft are NOT different
        if current_versions['published-branch'] == current_versions['draft-branch']:
            self.context['msg'] = _("Course is already in published state.")
            logging.info(
                "Course is already in published state. %s attempted to force publish the course %s.",
                request.user,
                course_id,
                exc_info=True
            )
            return self.render_response()

        self.context['current_versions'] = current_versions

        if is_dry_run:
            logging.info(
                "%s dry ran force publish the course %s.",
                request.user,
                course_id,
                exc_info=True
            )
            return self.render_response()
        updated_versions = source_store.force_publish_course(
            course_usage_key, request.user, commit=True
        )
        if not updated_versions:
            self.context['msg'] = _("Could not publish course.")
            logging.info(
                "Could not publish course. %s attempted to force publish the course %s.",
                request.user,
                course_id,
                exc_info=True
            )
            return self.render_response()

        self.context['updated_versions'] = updated_versions
        msg = "Published branch version changed from {published_prev} to {published_new}.".format(
            published_prev=current_versions['published-branch'],
            published_new=updated_versions['published-branch']
        )
        logging.info(
            "%s %s published course %s forcefully.",
            msg,
            request.user,
            course_id,
            exc_info=True
        )
        return self.render_response()
