-- Uploads that never finished (app closed, network lost) stop holding a library slot
-- once their presigned URL (1 h) can no longer be used.
create or replace function public.video_slots_used(p_owner uuid) returns integer
language sql stable security definer set search_path = '' as $$
  select count(*)::integer from public.videos
   where owner_id = p_owner
     and not is_draft
     and (status in ('processing', 'ready')
          or (status = 'uploading' and created_at > now() - interval '2 hours'))
$$;
revoke execute on function public.video_slots_used(uuid) from public, anon, authenticated;
