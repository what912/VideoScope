# Optional Supabase authentication

VideoScope's browser analysis is anonymous and local by default. Supabase is
optional: when either public setting is absent, the site uses an unavailable
authentication adapter and does not initialize a Supabase client or make an
authentication request.

## Browser configuration

Copy `site/.env.example` to `site/.env.local` and set only the public project
values:

```text
VITE_SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
VITE_SUPABASE_ANON_KEY=YOUR_PUBLIC_ANON_KEY
```

Never put a service-role key in the browser, a Vite variable, GitHub Pages
configuration, or this repository. Vite variables are included in the public
browser bundle.

Apply
`migrations/202607290001_public_site_auth_and_reports.sql` to the intended
Supabase project. It creates user-owned `profiles`, `report_index`, and
`shared_reports` tables and enables row-level security. Authentication alone
does not upload local video files or browser evidence.

## Optional sanitized report sharing

Sharing has a separate, default-off gate. It is available only when all of the
following are true:

```text
VITE_SUPABASE_SHARE_ENABLED=true
VITE_SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
VITE_SUPABASE_ANON_KEY=YOUR_PUBLIC_ANON_KEY
```

The user must also have an authenticated session and confirm the final consent
checkbox. If any gate is missing, the browser shows `Not configured` and does
not initialize or call the sharing client.

Before inserting a `shared_reports` row, the browser removes the original
filename, local report identifiers, absolute paths, object/data/blob/file
URLs, runtime cache fields, unselected evidence, and the prompt unless it is
separately opted in. A user-entered public title may be included. Evidence
selection transmits only its sanitized timestamp, description, type, and safe
metadata. Evidence image upload is disabled pending a separate storage-policy
security review. The original video is never inserted or uploaded.

Owners can create and read their own records and revoke them by setting
`revoked_at`. Anonymous visitors have no table-select grant. They can request
one unpredictable public UUID through the security-definer
`get_shared_report(requested_public_id)` function, which returns a result only
when it is neither revoked nor expired. There is no anonymous listing policy
or public-ID enumeration endpoint.

For later revocation, the browser keeps a minimal, account-scoped local index
containing only the public ID, creation and optional expiry time, local report
ID, and display title. It never stores the report JSON, source video, evidence
images, or prompt in this index. The report sharing dialog restores these
records after a refresh and lets the signed-in owner revoke them. This index is
local browser state, not a server-side owner listing, and is not synchronized
across browsers or devices.

## Redirect URLs

Add both application callbacks to the Supabase Auth URL configuration:

```text
http://localhost:5173/VideoScope/auth/callback
https://what912.github.io/VideoScope/auth/callback
```

For the GitHub OAuth application, use the callback URL displayed by the
Supabase dashboard. It has this form:

```text
https://YOUR_PROJECT_REF.supabase.co/auth/v1/callback
```

The GitHub application sends the provider response to Supabase; Supabase then
returns the browser to one of the two VideoScope redirect URLs above.

## Verification

After configuring a real project, manually verify:

1. email magic-link sign-in;
2. GitHub OAuth sign-in;
3. session restoration after a page refresh;
4. sign-out;
5. anonymous analysis in a signed-out private window;
6. row-level policies with two separate test accounts.

Real provider verification requires project credentials and is not part of
the offline base test suite.

## Account deletion

The static browser client cannot safely hold the privileged credentials
needed to delete an authentication user. There is currently no private,
verified account-deletion request channel for this project. Public GitHub
Issues are not an acceptable substitute: users must not post email addresses,
account identifiers, magic links, access tokens, reports, or other private
account data in an issue.

**Production authentication must remain disabled until the following external
release blocker is completed:**

1. The maintainer selects a private request channel, publishes its exact URL
   in the site Privacy page and this document, and documents who can access
   requests.
2. The flow verifies account ownership without asking for a password, magic
   link, access token, or other reusable credential. A fresh authenticated
   session or a one-time confirmation sent by the configured identity provider
   is the expected proof.
3. A separately reviewed privileged server function or documented Supabase
   administrator procedure deletes the user's `report_index` and `profiles`
   rows, deletes the Supabase Auth user, and revokes active sessions. No
   service-role credential may enter the static client.
4. The operator records only the request time, completion time, and a
   non-sensitive audit reference, then confirms completion through the same
   private channel.
5. The complete process is tested with two disposable accounts, including
   ownership rejection, successful deletion, session invalidation, and
   confirmation that one account cannot affect the other.

Until all five items are verified, leave `VITE_SUPABASE_URL` and
`VITE_SUPABASE_ANON_KEY` unset in the production deployment. Anonymous local
analysis remains available without them.
