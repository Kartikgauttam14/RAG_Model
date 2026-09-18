# Security model

JWT access tokens and Argon2 password hashes protect the API. Roles are `user`, `editor`, and `admin`; retrieval adds tenant, public/tenant/user/role scope filters before candidate results reach the model.

Uploads have size, content-type, extension and signature checks, content hashing, duplicate prevention, and an optional malware command hook. URL ingestion remains disabled until an allow-list is configured; its validator rejects credentials and resolved private, loopback, link-local, multicast, reserved and unspecified IP addresses.

Retrieved text is wrapped as untrusted evidence and injection patterns are recorded for the model context. Admin actions create audit records. Never put provider tokens in the frontend. Run `/metrics` only on an internal network or behind authenticated monitoring infrastructure.
