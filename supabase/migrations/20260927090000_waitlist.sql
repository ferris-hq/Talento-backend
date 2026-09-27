-- The website's waitlist.
--
-- The site is a static page with no server of its own, so it inserts straight into this table
-- with the publishable (anon) key. That means the policy has to be the whole defence: anyone
-- may insert a row, nobody may read one back, and the column constraints keep the rows sane.

create table if not exists public.waitlist (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  email text not null,
  role text not null,
  sport text,
  region text,
  source text not null default 'website',
  constraint waitlist_email_shape check (
    email ~* '^[^@[:space:]]+@[^@[:space:]]+\.[a-z]{2,}$' and length(email) between 5 and 254
  ),
  constraint waitlist_role_allowed check (role in ('athlete', 'coach', 'other')),
  constraint waitlist_sport_len check (sport is null or length(sport) <= 40),
  constraint waitlist_region_len check (region is null or length(region) <= 60),
  constraint waitlist_source_len check (length(source) <= 40)
);

-- One row per address; a second sign-up should not create a duplicate.
create unique index if not exists waitlist_email_key on public.waitlist (lower(email));

create index if not exists waitlist_created_at_idx on public.waitlist (created_at desc);

alter table public.waitlist enable row level security;

-- Insert only, and only for the shapes above. No select, update or delete for anon or
-- authenticated: the list is read with the service role (admin dashboard, SQL, exports).
drop policy if exists "waitlist insert from the website" on public.waitlist;
create policy "waitlist insert from the website"
  on public.waitlist
  for insert
  to anon, authenticated
  with check (true);

comment on table public.waitlist is
  'Sign-ups from talentoafrica.com. Insert-only for anon; read with the service role.';
