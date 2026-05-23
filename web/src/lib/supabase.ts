// Browser-side Supabase client — uses the PUBLISHABLE key (new-model
// equivalent of the legacy anon JWT). Publishable key is intentionally
// public; RLS policies on curator.{items,sources} are what guard access.
//
// This site queries the `curator` schema exclusively, so the schema is
// hard-coded rather than passed per call.

import { createClient } from '@supabase/supabase-js';

export function makeSupabase() {
  const url = import.meta.env.PUBLIC_SUPABASE_URL;
  const key = import.meta.env.PUBLIC_SUPABASE_PUBLISHABLE_KEY;
  if (!url || !key) {
    throw new Error(
      'PUBLIC_SUPABASE_URL and PUBLIC_SUPABASE_PUBLISHABLE_KEY must be set at build time ' +
        '(e.g. `doppler run --project scrape --config dev -- npm run build`)',
    );
  }
  return createClient(url, key, {
    auth: { persistSession: false, autoRefreshToken: false },
    db: { schema: 'curator' },
  });
}
