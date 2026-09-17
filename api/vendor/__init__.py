"""The vendor API: the surface external assessors and installers use.

Isolated from the CRM three independent ways, copied from ``api/partner/``
because that design has already proved itself:

1. an nginx vhost serving only this hostname;
2. :class:`api.middleware.VendorHostMiddleware`, which swaps ``request.urlconf``
   so CRM routes do not EXIST here rather than merely being forbidden;
3. authentication deliberately kept OUT of ``DEFAULT_AUTHENTICATION_CLASSES``, so
   a vendor token presented to a CRM endpoint is unrecognised (401) rather than
   merely unauthorised -- and an agent JWT is unrecognised here.

Any one layer would do the job. Three means a single misconfiguration cannot
expose the CRM.
"""
