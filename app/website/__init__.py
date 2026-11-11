"""Enquiries from the contact form on hamdaz.com.

Two routers, kept apart for the same reason the careers ones are:

* ``app.website.public`` — the one door the website posts to. Unauthenticated,
  mounted outside the API prefix, and able to do exactly one thing: leave a
  message.
* ``app.website.router`` — the sales side under the API prefix, gated by the
  ``website_enquiries`` module: read the messages, take them on, note what
  happened.
"""

from __future__ import annotations

from typing import Final

MODULE_KEY: Final = "website_enquiries"
MODULE_NAME: Final = "Website Enquiries"
