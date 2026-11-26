# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
LoKI's catalogues for the option ``-c`` of ``essbatch``. It needs the extra
``scicat``::

    essbatch -c ess.instruments.loki.scicat:staging --proposal 336349 files

Each catalogue logs in to SciCat by single sign-on, once per command. The login
opens a browser on this machine. With ``SCITACEAN_OAUTH_FLOW=device``, it prints
a URL to open on any machine.
"""

import scitacean

from ess.apps.scicat import Catalogue


def staging(proposal: str) -> Catalogue:
    """The staging SciCat of ESS."""
    return _logged_in('staging.ess')


def production(proposal: str) -> Catalogue:
    """The SciCat of ESS."""
    return _logged_in('ess')


def _logged_in(profile: str) -> Catalogue:
    return Catalogue(client=scitacean.Client.login(profile), instrument='loki')
