API endpoints implemented in prototype:

- POST /auth/register -> create a pending user and send an OTP to a deliverable email address
- POST /auth/verify-otp -> verify the OTP and receive a JWT
- POST /auth/token -> login and receive a JWT after verification
- GET /auth/me -> inspect the current token user, including role and verification state
- GET /admin/users -> admin-only user listing
- POST /admin/users -> admin-only provisioning for admin or supplier accounts
- GET /user -> user portal page
- GET /admin -> admin portal page
- GET /supplier -> supplier portal page
- GET /supplier/movement -> supplier-only inventory movement summary
- POST /scans -> capture and persist a product scan snapshot
- GET /scans/me -> current-user scan history
- GET /admin/scans -> admin scan history
- POST /suppliers -> create supplier
- POST /products -> create product
- POST /batches -> create batch (stores quantity_remaining)
- POST /sales -> record a sale (FEFO deduction across batches)
- GET /products/{product_id}/rop -> compute ROP using recent velocity
- GET /inventory -> list every product with aggregate stock and inventory status
- GET /products/{product_id}/batches -> list batches with FEFO-oriented status
- GET / and GET /dashboard -> render a small browser dashboard for the same data

Run with:
```
uvicorn main:app --reload
```
