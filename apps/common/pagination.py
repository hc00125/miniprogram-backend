from rest_framework.pagination import PageNumberPagination


class OptionalPageNumberPagination(PageNumberPagination):
    """Only paginate when the client explicitly passes ?page=...

    This keeps existing mini-program endpoints that expect a plain list compatible,
    while still providing a global pagination implementation for new list APIs.
    """

    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100

    def paginate_queryset(self, queryset, request, view=None):
        if self.page_query_param not in request.query_params:
            return None
        return super().paginate_queryset(queryset, request, view)
