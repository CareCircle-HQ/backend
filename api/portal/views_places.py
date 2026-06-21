"""Portal-scoped wrappers for the Google Places proxy.

Reuse the existing :mod:`api.views_places` logic but gate access on the portal
agent group (:class:`IsPortalAgent`) so the support portal can power the
delivery-address autocomplete the same way the extension powers the doctor
address.
"""

from ..views_places import PlacesAutocompleteView, PlacesDetailsView
from .permissions import IsPortalAgent


class PortalPlacesAutocompleteView(PlacesAutocompleteView):
    permission_classes = [IsPortalAgent]


class PortalPlacesDetailsView(PlacesDetailsView):
    permission_classes = [IsPortalAgent]
