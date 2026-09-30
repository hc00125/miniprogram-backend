from django.urls import path
from .views import catalog

app_name = 'gifts'
from . import transaction_views as tx
urlpatterns = [path('catalog/', catalog, name='catalog'),
    path('quotes/', tx.quotes, name='quotes'), path('purchases/', tx.buy, name='purchases'),
    path('capabilities/', tx.capabilities, name='capabilities'),
    path('inventory/quotes/', tx.inventory_quotes, name='inventory-quotes'),
    path('purchase-records/', tx.purchase_list, name='purchase-records'),
    path('purchases/by-key/', tx.purchase_by_key, name='purchase-by-key'),
    path('transfers/by-key/', tx.transfer_by_key, name='transfer-by-key'),
    path('purchases/<str:purchase_no>/', tx.purchase_detail, name='purchase-detail'),
    path('inventory/', tx.inventory, name='inventory'), path('transfers/', tx.send, name='transfers'),
    path('sent/', tx.sent, name='sent'), path('received/', tx.received, name='received'),
    path('earnings/', tx.earnings, name='earnings')]

