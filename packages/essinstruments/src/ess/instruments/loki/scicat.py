# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
LoKI's catalogues, each a function from a proposal to a
:class:`~ess.apps.scicat.Catalogue` that logs in to SciCat by single sign-on,
for the option ``-c`` of ``essbatch``. It needs the extra ``scicat``::

    essbatch -c ess.instruments.loki.scicat:staging --proposal 336349 files

The login opens a browser on this machine, or, with
``SCITACEAN_OAUTH_FLOW=device``, prints a URL to open on any. Each command
logs in anew.
"""

import scitacean

from ess.apps.scicat import Catalogue


def staging(proposal: str) -> Catalogue:
    """The staging SciCat of ESS, which holds the data of ESS for now."""
    return _logged_in('staging.ess')


def production(proposal: str) -> Catalogue:
    """The SciCat of ESS."""
    return _logged_in('ess')


def _logged_in(profile: str) -> Catalogue:
    return Catalogue(client=scitacean.Client.login(profile), instrument='loki')
