# -*- coding: utf-8 -*-
#
# Copyright 2025-present Bitergia
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import logging

import dateutil.relativedelta
import requests

from django.conf import settings
from django.db.models import (Q, Subquery)

from requests_oauth2client import OAuth2Client
from requests_oauth2client.tokens import ExpiredAccessToken

from grimoirelab_toolkit.datetime import (
    str_to_datetime,
    datetime_utcnow
)
from sortinghat.core.importer.backend import IdentitiesImporter
from sortinghat.core.importer.models import (
    Individual,
    Identity,
    Enrollment,
    Organization,
    Profile,
)
from sortinghat.core import api
from sortinghat.core import models as sh_models


# Data source types
ECLIPSE_SOURCE = "eclipsefdn"
GITHUB_SOURCE = "github"


logger = logging.getLogger(__name__)


class EclipseFoundationAccountsImporter(IdentitiesImporter):
    """Imports identities from the Eclipse Foundation platform.

    The importer fetches and stores in the database identities
    created or updated after the given date (`from_date`) parameter.
    Currently, it can only import identities updated since a year ago.
    When no date is given, it will import all the identities updated
    since last year.

    Each individual created after importing will have two identities:
    one with source set as 'eclipsefdn' that includes their name, email
    and username as it comes from the platform, and a second one with
    source 'github' only when the github user was set by the identity
    on the Eclipse profile.

    :param ctx: SortingHat context
    :param url: not used on this importer
    :param from_date: start fetching identities updated from this date

    :raises ValueError: when the date is older than one year ago
    """
    NAME = "EclipseFoundation"

    def __init__(self, ctx, url, from_date=None):
        super().__init__(ctx, url)

        min_date = datetime_utcnow() - dateutil.relativedelta.relativedelta(years=1)

        if not from_date:
            self.from_date = min_date
        elif isinstance(from_date, str):
            self.from_date = str_to_datetime(from_date)
        else:
            self.from_date = from_date

        if self.from_date < min_date:
            msg = (
                "Invalid 'from_date' value. It can only import identities updated since a year ago."
                "from_date=" + from_date
            )
            logger.error(msg)
            raise ValueError(msg)

    def get_individuals(self):
        """Get the individuals from the Eclipse Foundation platform."""

        user_id = getattr(settings, 'ECLIPSE_FOUNDATION_USER_ID', None)
        password = getattr(settings, 'ECLIPSE_FOUNDATION_PASSWORD', None)

        client = EclipseFoundationAPIClient()
        client.login(user_id, password)

        epoch = int(self.from_date.timestamp())

        # Fetch accounts pages
        for account in client.fetch_accounts(epoch=epoch):
            ef_profile = client.fetch_account_profile(account['name'])

            if not ef_profile:
                continue

            individual = Individual(uuid=ef_profile['uid'])

            name = ef_profile['first_name'] + ' ' + ef_profile['last_name']
            email = ef_profile['mail']

            prf = Profile()
            prf.name = name
            prf.email = email

            individual.profile = prf

            eclipse_id = Identity(
                source=ECLIPSE_SOURCE,
                name=name,
                email=email,
                username=ef_profile['name'],
            )
            individual.identities.append(eclipse_id)

            if ef_profile['github_handle']:
                idt = Identity(
                    source=GITHUB_SOURCE,
                    name=name,
                    username=ef_profile['github_handle'],
                    email=email,
                )
                individual.identities.append(idt)

            # Fetch enrollments for the identity. If no enrollment is set
            # use the organization field from the profile, if set.
            employment_history = client.fetch_employment_history(account['name'])

            if employment_history:
                for employment in employment_history:
                    org = Organization(name=employment['organization_name'])
                    start, end = None, None

                    if employment['start']:
                        start = str_to_datetime(employment['start'])
                    if employment['end']:
                        end = str_to_datetime(employment['end'])

                    enr = Enrollment(org, start=start, end=end)
                    individual.enrollments.append(enr)

            if not individual.enrollments:
                company = ef_profile.get('org', None)
                if company:
                    org = Organization(name=company)
                    enr = Enrollment(org)
                    individual.enrollments.append(enr)

            logger.info(f"Eclipse account processed; account={account['name']}; changed={account['changed']}")

            yield individual

    def post_process_individual(self, individual, uuid):
        """Post processing for Eclipse identities.

        The method tries to find Eclipse or GitHub identities
        already imported to merge them with the given individual.
        When that happens the profile will be the Eclipse individual's
        one.
        """
        eclipse_identity = None

        for identity in individual.identities:
            if identity.source == ECLIPSE_SOURCE and identity.email and identity.username:
                eclipse_identity = identity
                break

        if not eclipse_identity:
            return

        query = sh_models.Individual.objects.filter(
            mk__in=Subquery(
                sh_models.Identity.objects.filter(
                    Q(email=eclipse_identity.email) |
                    (Q(username=eclipse_identity.username) & Q(source='github'))
                ).exclude(uuid=uuid).values_list('individual__mk')
            )
        ).exclude(mk=uuid).values_list('mk')

        from_uuids = [entry[0] for entry in query.all()]

        if from_uuids:
            api.merge(self.ctx, from_uuids, uuid)


class EclipseFoundationAPIClient:
    """Eclipse Foundation's Profile API client."""

    ECLIPSE_API_URL = "https://api.eclipse.org"
    ECLIPSE_ACCOUNTS_URL = "https://accounts.eclipse.org"
    OAUTH_TOKEN_ENDPOINT = "https://accounts.eclipse.org/oauth2/token"
    ECLIPSE_SCOPE = "eclipsefdn_view_all_profiles"

    MAX_RETRIES = 3

    def __init__(self):
        self.token = None
        self.user_id = None
        self.password = None

    def login(self, user_id, password):
        """Login on the Eclipse platform.

        The authentication method is OAuth2. We use the scope
        "eclipsefdn_view_all_profiles" that will allow us to
        fetch all the info about profiles/identities.
        """
        self.user_id = user_id
        self.password = password
        self.token = self._authenticate(
            self.user_id,
            self.password,
            self.ECLIPSE_SCOPE,
        )

    def logout(self):
        """Logout from the Eclipse platform."""

        self.token = None

    def fetch_accounts(self, epoch):
        """Fetch accounts updated from a given UNIX time."""

        page = 1
        total_accounts = 0

        logger.info(f"Fetching accounts from API; url={self.ECLIPSE_ACCOUNTS_URL}, epoch={epoch}")

        while True:
            url = f"{self.ECLIPSE_ACCOUNTS_URL}/account/updated"
            params = {
                'since': epoch,
                'page': page
            }

            logger.debug(f"Fetching accounts from API; url={url}, params={params}")
            data = self._fetch(url, params=params)

            for account in data['result']:
                yield account

            naccounts = len(data['result'])
            total_accounts += naccounts

            logger.debug(f"Accounts from API fetched; url={url}, params={params}, naccounts={naccounts}")

            if page >= data['pagination']['result_end']:
                break

            page += 1

        logger.info(f"Accounts fetched from API; url={url}, epoch={epoch}, total_accounts={total_accounts}")

    def fetch_account_profile(self, eclipsefdn_id):
        """Get the profile of the given identity."""

        url = f"{self.ECLIPSE_API_URL}/account/profile/{eclipsefdn_id}"
        data = self._fetch(url)
        logger.info(f"Profile fetched; url={url}, eclipsefdn_id={eclipsefdn_id}")
        return data

    def fetch_employment_history(self, eclipsefdn_id):
        """Get the employment history of the given identity."""

        url = f"{self.ECLIPSE_API_URL}/account/profile/{eclipsefdn_id}/employment-history"
        data = self._fetch(url)
        logger.info(f"Employment history fetched; url={url}, eclipsefdn_id={eclipsefdn_id}")
        return data

    def _fetch(self, url, params=None):
        """Generic query to Eclipse usr API."""

        try:
            data = self._fetch_retry(url, params)
        except requests.exceptions.HTTPError as error:
            # Ignore 5xx errors
            if 500 <= error.response.status_code < 600:
                msg = (
                    f"Unable to fetch {url}"
                    f"Server error: {error.response.status_code} - {error.response.reason}."
                    "Skipping"
                )
                logger.error(msg)
                return None
            else:
                raise error

        return data

    def _fetch_retry(self, url, params=None):
        """Fetch URL retrying in case of 403 or 500 errors.

        When getting a 403 error, the method will try to authenticate
        again in case the OAuth2 token has expired.
        """
        retries = 0
        max_retries = self.MAX_RETRIES

        while retries < max_retries:
            try:
                response = requests.get(url, params=params, auth=self.token)
            except ExpiredAccessToken:
                # Refresh token and try again
                self.login(self.user_id, self.password)
                retries += 1
                continue

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 403:
                if self.token.expires_at <= datetime_utcnow():
                    self.login(self.user_id, self.password)
                retries += 1
            elif 500 <= response.status_code < 600:
                # Errors could have been related to server overloading
                retries += 1
            else:
                response.raise_for_status()

        response = requests.get(url, params=params, auth=self.token)
        response.raise_for_status()

        return response

    def _authenticate(self, client_id, client_secret, scope):
        """Authenticate using OAuth2.

        After authenticating, returns a Bearer token that can be used
        in the API requests.
        """
        oauth2client = OAuth2Client(
            token_endpoint=self.OAUTH_TOKEN_ENDPOINT,
            client_id=client_id,
            client_secret=client_secret,
        )
        token = oauth2client.client_credentials(scope=scope)

        return token
