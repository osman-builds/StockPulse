# This file has been removed.
# It was a duplicate of services/sales_service.py with a critical bug:
#   Sale(timestamp=None) — caused NULL timestamps breaking ROP velocity queries.
# Use services/sales_service.process_sale() instead.
raise ImportError(
    "sale_service.py has been removed. Use services/sales_service.process_sale() instead."
)