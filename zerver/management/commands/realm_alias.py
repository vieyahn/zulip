from __future__ import absolute_import
from __future__ import print_function

from typing import Any

from argparse import ArgumentParser
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from zerver.models import Realm, RealmAlias, get_realm, can_add_alias
from zerver.lib.actions import get_realm_aliases
from zerver.lib.domains import validate_domain
import sys

class Command(BaseCommand):
    help = """Manage aliases for the specified realm"""

    def add_arguments(self, parser):
        # type: (ArgumentParser) -> None
        parser.add_argument('-r', '--realm',
                            dest='string_id',
                            type=str,
                            required=True,
                            help='The subdomain or string_id of the realm.')
        parser.add_argument('--op',
                            dest='op',
                            type=str,
                            default="show",
                            help='What operation to do (add, show, remove).')
        parser.add_argument('alias', metavar='<alias>', type=str, nargs='?',
                            help="alias to add or remove")

    def handle(self, *args, **options):
        # type: (*Any, **str) -> None
        realm = get_realm(options["string_id"])
        if options["op"] == "show":
            print("Aliases for %s:" % (realm.domain,))
            for alias in get_realm_aliases(realm):
                print(alias["domain"])
            sys.exit(0)

        domain = options['alias'].strip().lower()
        try:
            validate_domain(domain)
        except ValidationError as e:
            print(e.messages[0])
            sys.exit(1)
        if options["op"] == "add":
            if not can_add_alias(domain):
                print("A Realm already exists for this domain, cannot add it as an alias for another realm!")
                sys.exit(1)
            RealmAlias.objects.create(realm=realm, domain=domain)
            sys.exit(0)
        elif options["op"] == "remove":
            RealmAlias.objects.get(realm=realm, domain=domain).delete()
            sys.exit(0)
        else:
            self.print_help("./manage.py", "realm_alias")
            sys.exit(1)
