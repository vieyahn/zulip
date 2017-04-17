from __future__ import absolute_import
from typing import Any, List, Dict, Optional, Text

from django.conf import settings
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect, HttpResponse, HttpRequest
from django.shortcuts import redirect
from django.utils import translation
from django.utils.cache import patch_cache_control
from six.moves import zip_longest, zip, range

from version import ZULIP_VERSION
from zerver.decorator import zulip_login_required
from zerver.forms import ToSForm
from zerver.models import Message, UserProfile, Stream, Subscription, Huddle, \
    Recipient, Realm, UserMessage, DefaultStream, RealmEmoji, RealmAlias, \
    RealmFilter, \
    PreregistrationUser, get_client, UserActivity, \
    UserPresence, get_recipient, name_changes_disabled, email_to_username, \
    list_of_domains_for_realm
from zerver.lib.actions import update_user_presence, do_change_tos_version, \
    do_events_register, do_update_pointer, get_cross_realm_dicts, realm_user_count
from zerver.lib.avatar import avatar_url
from zerver.lib.i18n import get_language_list, get_language_name, \
    get_language_list_for_templates
from zerver.lib.push_notifications import num_push_devices_for_user
from zerver.lib.streams import access_stream_by_name
from zerver.lib.utils import statsd, get_subdomain
from zproject.backends import password_auth_enabled
from zproject.jinja2 import render_to_response

import calendar
import datetime
import logging
import re
import simplejson
import time

@zulip_login_required
def accounts_accept_terms(request):
    # type: (HttpRequest) -> HttpResponse
    if request.method == "POST":
        form = ToSForm(request.POST)
        if form.is_valid():
            do_change_tos_version(request.user, settings.TOS_VERSION)
            return redirect(home)
    else:
        form = ToSForm()

    email = request.user.email
    special_message_template = None
    if request.user.tos_version is None and settings.FIRST_TIME_TOS_TEMPLATE is not None:
        special_message_template = 'zerver/' + settings.FIRST_TIME_TOS_TEMPLATE
    return render_to_response(
        'zerver/accounts_accept_terms.html',
        {'form': form,
         'email': email,
         'special_message_template': special_message_template},
        request=request)

def approximate_unread_count(user_profile):
    # type: (UserProfile) -> int
    not_in_home_view_recipients = [sub.recipient.id for sub in
                                   Subscription.objects.filter(
                                       user_profile=user_profile, in_home_view=False)]

    # TODO: We may want to exclude muted messages from this count.
    #       It was attempted in the past, but the original attempt
    #       was broken.  When we re-architect muting, we may
    #       want to to revisit this (see git issue #1019).
    return UserMessage.objects.filter(
        user_profile=user_profile, message_id__gt=user_profile.pointer).exclude(
        message__recipient__type=Recipient.STREAM,
        message__recipient__id__in=not_in_home_view_recipients).exclude(
        flags=UserMessage.flags.read).count()

def sent_time_in_epoch_seconds(user_message):
    # type: (UserMessage) -> float
    # user_message is a UserMessage object.
    if not user_message:
        return None
    # We have USE_TZ = True, so our datetime objects are timezone-aware.
    # Return the epoch seconds in UTC.
    return calendar.timegm(user_message.message.pub_date.utctimetuple())

def home(request):
    # type: (HttpRequest) -> HttpResponse
    if not settings.SUBDOMAINS_HOMEPAGE:
        return home_real(request)

    # If settings.SUBDOMAINS_HOMEPAGE, sends the user the landing
    # page, not the login form, on the root domain

    subdomain = get_subdomain(request)
    if subdomain != "":
        return home_real(request)

    return render_to_response('zerver/hello.html',
                              request=request)

@zulip_login_required
def home_real(request):
    # type: (HttpRequest) -> HttpResponse
    # We need to modify the session object every two weeks or it will expire.
    # This line makes reloading the page a sufficient action to keep the
    # session alive.
    request.session.modified = True

    user_profile = request.user
    request._email = request.user.email
    request.client = get_client("website")

    # If a user hasn't signed the current Terms of Service, send them there
    if settings.TERMS_OF_SERVICE is not None and settings.TOS_VERSION is not None and \
       int(settings.TOS_VERSION.split('.')[0]) > user_profile.major_tos_version():
        return accounts_accept_terms(request)

    narrow = [] # type: List[List[Text]]
    narrow_stream = None
    narrow_topic = request.GET.get("topic")
    if request.GET.get("stream"):
        try:
            narrow_stream_name = request.GET.get("stream")
            (narrow_stream, ignored_rec, ignored_sub) = access_stream_by_name(
                user_profile, narrow_stream_name)
            narrow = [["stream", narrow_stream.name]]
        except Exception:
            logging.exception("Narrow parsing")
        if narrow_stream is not None and narrow_topic is not None:
            narrow.append(["topic", narrow_topic])

    register_ret = do_events_register(user_profile, request.client,
                                      apply_markdown=True, narrow=narrow)
    user_has_messages = (register_ret['max_message_id'] != -1)

    # Reset our don't-spam-users-with-email counter since the
    # user has since logged in
    if user_profile.last_reminder is not None:
        user_profile.last_reminder = None
        user_profile.save(update_fields=["last_reminder"])

    # Brand new users get the tutorial
    needs_tutorial = settings.TUTORIAL_ENABLED and \
        user_profile.tutorial_status != UserProfile.TUTORIAL_FINISHED

    first_in_realm = realm_user_count(user_profile.realm) == 1
    # If you are the only person in the realm and you didn't invite
    # anyone, we'll continue to encourage you to do so on the frontend.
    prompt_for_invites = first_in_realm and \
        not PreregistrationUser.objects.filter(referred_by=user_profile).count()

    if user_profile.pointer == -1 and user_has_messages:
        # Put the new user's pointer at the bottom
        #
        # This improves performance, because we limit backfilling of messages
        # before the pointer.  It's also likely that someone joining an
        # organization is interested in recent messages more than the very
        # first messages on the system.

        register_ret['pointer'] = register_ret['max_message_id']
        user_profile.last_pointer_updater = request.session.session_key

    if user_profile.pointer == -1:
        latest_read = None
    else:
        try:
            latest_read = UserMessage.objects.get(user_profile=user_profile,
                                                  message__id=user_profile.pointer)
        except UserMessage.DoesNotExist:
            # Don't completely fail if your saved pointer ID is invalid
            logging.warning("%s has invalid pointer %s" % (user_profile.email, user_profile.pointer))
            latest_read = None

    desktop_notifications_enabled = user_profile.enable_desktop_notifications
    if narrow_stream is not None:
        desktop_notifications_enabled = False

    if user_profile.realm.notifications_stream:
        notifications_stream = user_profile.realm.notifications_stream.name
    else:
        notifications_stream = ""

    # Set default language and make it persist
    default_language = register_ret['default_language']
    url_lang = '/{}'.format(request.LANGUAGE_CODE)
    if not request.path.startswith(url_lang):
        translation.activate(default_language)

    request.session[translation.LANGUAGE_SESSION_KEY] = default_language

    # Pass parameters to the client-side JavaScript code.
    # These end up in a global JavaScript Object named 'page_params'.
    page_params = dict(
        zulip_version         = ZULIP_VERSION,
        share_the_love        = settings.SHARE_THE_LOVE,
        development_environment = settings.DEVELOPMENT,
        debug_mode            = settings.DEBUG,
        test_suite            = settings.TEST_SUITE,
        poll_timeout          = settings.POLL_TIMEOUT,
        login_page            = settings.HOME_NOT_LOGGED_IN,
        server_uri            = settings.SERVER_URI,
        realm_uri             = user_profile.realm.uri,
        maxfilesize           = settings.MAX_FILE_UPLOAD_SIZE,
        server_generation     = settings.SERVER_GENERATION,
        password_auth_enabled = password_auth_enabled(user_profile.realm),
        have_initial_messages = user_has_messages,
        subbed_info           = register_ret['subscriptions'],
        unsubbed_info         = register_ret['unsubscribed'],
        neversubbed_info      = register_ret['never_subscribed'],
        people_list           = register_ret['realm_users'],
        bot_list              = register_ret['realm_bots'],
        initial_pointer       = register_ret['pointer'],
        initial_presences     = register_ret['presences'],
        initial_servertime    = time.time(), # Used for calculating relative presence age
        fullname              = user_profile.full_name,
        email                 = user_profile.email,
        domain                = user_profile.realm.domain,
        domains               = list_of_domains_for_realm(user_profile.realm),
        realm_name            = register_ret['realm_name'],
        realm_invite_required = register_ret['realm_invite_required'],
        realm_invite_by_admins_only = register_ret['realm_invite_by_admins_only'],
        realm_authentication_methods = register_ret['realm_authentication_methods'],
        realm_create_stream_by_admins_only = register_ret['realm_create_stream_by_admins_only'],
        realm_add_emoji_by_admins_only = register_ret['realm_add_emoji_by_admins_only'],
        realm_allow_message_editing = register_ret['realm_allow_message_editing'],
        realm_message_content_edit_limit_seconds = register_ret['realm_message_content_edit_limit_seconds'],
        realm_restricted_to_domain = register_ret['realm_restricted_to_domain'],
        realm_default_language = register_ret['realm_default_language'],
        realm_waiting_period_threshold = register_ret['realm_waiting_period_threshold'],
        enter_sends           = user_profile.enter_sends,
        user_id               = user_profile.id,
        left_side_userlist    = register_ret['left_side_userlist'],
        default_language      = register_ret['default_language'],
        default_language_name = get_language_name(register_ret['default_language']),
        language_list_dbl_col = get_language_list_for_templates(register_ret['default_language']),
        language_list         = get_language_list(),
        referrals             = register_ret['referrals'],
        realm_emoji           = register_ret['realm_emoji'],
        needs_tutorial        = needs_tutorial,
        first_in_realm        = first_in_realm,
        prompt_for_invites    = prompt_for_invites,
        notifications_stream  = notifications_stream,
        cross_realm_bots      = list(get_cross_realm_dicts()),
        use_websockets        = settings.USE_WEBSOCKETS,

        # Stream message notification settings:
        stream_desktop_notifications_enabled = user_profile.enable_stream_desktop_notifications,
        stream_sounds_enabled = user_profile.enable_stream_sounds,

        # Private message and @-mention notification settings:
        desktop_notifications_enabled = desktop_notifications_enabled,
        sounds_enabled = user_profile.enable_sounds,
        enable_offline_email_notifications = user_profile.enable_offline_email_notifications,
        pm_content_in_desktop_notifications = user_profile.pm_content_in_desktop_notifications,
        enable_offline_push_notifications = user_profile.enable_offline_push_notifications,
        enable_online_push_notifications = user_profile.enable_online_push_notifications,
        twenty_four_hour_time = register_ret['twenty_four_hour_time'],
        enable_digest_emails  = user_profile.enable_digest_emails,
        event_queue_id        = register_ret['queue_id'],
        last_event_id         = register_ret['last_event_id'],
        max_message_id        = register_ret['max_message_id'],
        unread_count          = approximate_unread_count(user_profile),
        furthest_read_time    = sent_time_in_epoch_seconds(latest_read),
        save_stacktraces      = settings.SAVE_FRONTEND_STACKTRACES,
        alert_words           = register_ret['alert_words'],
        muted_topics          = register_ret['muted_topics'],
        realm_filters         = register_ret['realm_filters'],
        realm_default_streams = register_ret['realm_default_streams'],
        is_admin              = user_profile.is_realm_admin,
        can_create_streams    = user_profile.can_create_streams(),
        name_changes_disabled = name_changes_disabled(user_profile.realm),
        has_mobile_devices    = num_push_devices_for_user(user_profile) > 0,
        autoscroll_forever = user_profile.autoscroll_forever,
        default_desktop_notifications = user_profile.default_desktop_notifications,
        avatar_url            = avatar_url(user_profile),
        avatar_url_medium     = avatar_url(user_profile, medium=True),
        avatar_source         = user_profile.avatar_source,
        mandatory_topics      = user_profile.realm.mandatory_topics,
        show_digest_email     = user_profile.realm.show_digest_email,
        presence_disabled     = user_profile.realm.presence_disabled,
        is_zephyr_mirror_realm = user_profile.realm.is_zephyr_mirror_realm,
    )

    if narrow_stream is not None:
        # In narrow_stream context, initial pointer is just latest message
        recipient = get_recipient(Recipient.STREAM, narrow_stream.id)
        try:
            initial_pointer = Message.objects.filter(recipient=recipient).order_by('id').reverse()[0].id
        except IndexError:
            initial_pointer = -1
        page_params["narrow_stream"] = narrow_stream.name
        if narrow_topic is not None:
            page_params["narrow_topic"] = narrow_topic
        page_params["narrow"] = [dict(operator=term[0], operand=term[1]) for term in narrow]
        page_params["max_message_id"] = initial_pointer
        page_params["initial_pointer"] = initial_pointer
        page_params["have_initial_messages"] = (initial_pointer != -1)

    statsd.incr('views.home')
    show_invites = True

    # Some realms only allow admins to invite users
    if user_profile.realm.invite_by_admins_only and not user_profile.is_realm_admin:
        show_invites = False

    product_name = "Zulip"
    page_params['product_name'] = product_name
    request._log_data['extra'] = "[%s]" % (register_ret["queue_id"],)
    response = render_to_response('zerver/index.html',
                                  {'user_profile': user_profile,
                                   'page_params': simplejson.encoder.JSONEncoderForHTML().encode(page_params),
                                   'nofontface': is_buggy_ua(request.META.get("HTTP_USER_AGENT", "Unspecified")),
                                   'avatar_url': avatar_url(user_profile),
                                   'show_debug':
                                       settings.DEBUG and ('show_debug' in request.GET),
                                   'pipeline': settings.PIPELINE_ENABLED,
                                   'show_invites': show_invites,
                                   'is_admin': user_profile.is_realm_admin,
                                   'show_webathena': user_profile.realm.webathena_enabled,
                                   'enable_feedback': settings.ENABLE_FEEDBACK,
                                   'embedded': narrow_stream is not None,
                                   'product_name': product_name
                                   },
                                  request=request)
    patch_cache_control(response, no_cache=True, no_store=True, must_revalidate=True)
    return response

@zulip_login_required
def desktop_home(request):
    # type: (HttpRequest) -> HttpResponse
    return HttpResponseRedirect(reverse('zerver.views.home.home'))

def is_buggy_ua(agent):
    # type: (str) -> bool
    """Discrimiate CSS served to clients based on User Agent

    Due to QTBUG-3467, @font-face is not supported in QtWebKit.
    This may get fixed in the future, but for right now we can
    just serve the more conservative CSS to all our desktop apps.
    """
    return ("Humbug Desktop/" in agent or "Zulip Desktop/" in agent or "ZulipDesktop/" in agent) and \
        "Mac" not in agent
