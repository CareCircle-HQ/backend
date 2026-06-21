"""Pagination for the support portal.

A single page-number paginator used by every portal list endpoint. The
response envelope is explicit (count/page/page_size/total_pages/next/previous)
so the frontend's infinite-scroll hooks can drive `?page=` deterministically.
"""

from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class PortalPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data):
        return Response(
            {
                "count": self.page.paginator.count,
                "page": self.page.number,
                "page_size": self.get_page_size(self.request),
                "total_pages": self.page.paginator.num_pages,
                "next": self.get_next_link(),
                "previous": self.get_previous_link(),
                "results": data,
            }
        )
