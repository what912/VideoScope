create table if not exists public.profiles (
  user_id uuid primary key references auth.users (id) on delete cascade,
  display_name text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.report_index (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  analysis_id text not null,
  title text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, analysis_id)
);

create table if not exists public.shared_reports (
  public_id uuid primary key,
  owner_id uuid not null references auth.users (id) on delete cascade,
  report_json jsonb not null
    check (jsonb_typeof(report_json) = 'object')
    check (octet_length(report_json::text) <= 2097152),
  created_at timestamptz not null default now(),
  expires_at timestamptz,
  revoked_at timestamptz,
  check (expires_at is null or expires_at > created_at)
);

alter table public.profiles enable row level security;
alter table public.report_index enable row level security;
alter table public.shared_reports enable row level security;

create policy "profiles_select_own"
  on public.profiles
  for select
  using (auth.uid() = user_id);

create policy "profiles_insert_own"
  on public.profiles
  for insert
  with check (auth.uid() = user_id);

create policy "profiles_update_own"
  on public.profiles
  for update
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create policy "profiles_delete_own"
  on public.profiles
  for delete
  using (auth.uid() = user_id);

create policy "report_index_select_own"
  on public.report_index
  for select
  using (auth.uid() = user_id);

create policy "report_index_insert_own"
  on public.report_index
  for insert
  with check (auth.uid() = user_id);

create policy "report_index_update_own"
  on public.report_index
  for update
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create policy "report_index_delete_own"
  on public.report_index
  for delete
  using (auth.uid() = user_id);

create policy "shared_reports_select_own"
  on public.shared_reports
  for select
  using (auth.uid() = owner_id);

create policy "shared_reports_insert_own"
  on public.shared_reports
  for insert
  with check (auth.uid() = owner_id);

create policy "shared_reports_revoke_own"
  on public.shared_reports
  for update
  using (auth.uid() = owner_id)
  with check (auth.uid() = owner_id and revoked_at is not null);

revoke all on public.shared_reports from anon;
revoke all on public.shared_reports from authenticated;
grant select, insert on public.shared_reports to authenticated;
grant update (revoked_at) on public.shared_reports to authenticated;

create or replace function public.get_shared_report(
  requested_public_id uuid
)
returns table (
  public_id uuid,
  report_json jsonb,
  created_at timestamptz,
  expires_at timestamptz
)
language sql
stable
security definer
set search_path = ''
as $$
  select
    public.shared_reports.public_id,
    public.shared_reports.report_json,
    public.shared_reports.created_at,
    public.shared_reports.expires_at
  from public.shared_reports
  where public.shared_reports.public_id = requested_public_id
    and public.shared_reports.revoked_at is null
    and (
      public.shared_reports.expires_at is null
      or public.shared_reports.expires_at > pg_catalog.now()
    )
  limit 1;
$$;

revoke all on function public.get_shared_report(uuid) from public;
grant execute on function public.get_shared_report(uuid) to anon, authenticated;
