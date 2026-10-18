# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Expected failures reported by Foretoken operations."""


class DeploymentError(RuntimeError):
    """A Foretoken deployment cannot be applied or inspected."""
