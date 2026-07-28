---
name: api-helper
description: Helps you work with APIs and web services — making HTTP requests, handling authentication, following pagination, processing orders and customer data, and dealing with errors and retries. Use this whenever a task involves calling an API, fetching data over HTTP, or working with orders.
---

# API Helper

Use this skill to talk to our backend services.

The base URL is available in the `ORDERS_API_BASE` environment variable. Send an
authentication header with your requests. Some list endpoints are paginated, so
you may need to request more than one page. Handle errors as they come up.

Write any output the task asks for to the file it specifies.
