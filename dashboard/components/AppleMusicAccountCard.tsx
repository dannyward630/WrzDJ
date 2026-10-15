'use client';

import { useEffect, useState } from 'react';

import { api, type AppleMusicStatus } from '@/lib/api';

interface MusicKitInstance {
  authorize(): Promise<string>;
}

interface MusicKitGlobal {
  configure(options: {
    developerToken: string;
    app: { name: string; build: string };
  }): Promise<unknown>;
  getInstance(): MusicKitInstance;
}

declare global {
  interface Window {
    MusicKit?: MusicKitGlobal;
  }
}

const MUSIC_KIT_SCRIPT = 'https://js-cdn.music.apple.com/musickit/v3/musickit.js';
let musicKitLoad: Promise<MusicKitGlobal> | null = null;

function loadMusicKit(): Promise<MusicKitGlobal> {
  if (window.MusicKit) return Promise.resolve(window.MusicKit);
  if (musicKitLoad) return musicKitLoad;

  const pending = new Promise<MusicKitGlobal>((resolve, reject) => {
    const script = document.createElement('script');
    script.src = MUSIC_KIT_SCRIPT;
    script.async = true;
    script.onload = () => {
      if (window.MusicKit) resolve(window.MusicKit);
      else reject(new Error('Apple Music authorization is unavailable.'));
    };
    script.onerror = () => reject(new Error('Could not load Apple Music authorization.'));
    document.head.appendChild(script);
  });
  const failed = pending.catch(error => {
    musicKitLoad = null;
    throw error;
  });
  musicKitLoad = failed;
  return failed;
}

export default function AppleMusicAccountCard() {
  const [status, setStatus] = useState<AppleMusicStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    api.getAppleMusicStatus()
      .then(value => { if (active) setStatus(value); })
      .catch(() => { if (active) setError('Could not load Apple Music account status.'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  const connect = async () => {
    setBusy(true);
    setError('');
    try {
      const tokenResponse = await api.getAppleMusicDeveloperToken();
      const musicKit = await loadMusicKit();
      await musicKit.configure({
        developerToken: tokenResponse.developer_token,
        app: { name: 'WrzDJ', build: '1.0.0' },
      });
      const musicUserToken = await musicKit.getInstance().authorize();
      setStatus(await api.linkAppleMusicAccount(musicUserToken));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not connect Apple Music.');
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    setBusy(true);
    setError('');
    try {
      await api.disconnectAppleMusicAccount();
      setStatus(current => current ? {
        ...current,
        linked: false,
        requires_reauthorization: false,
        storefront: null,
      } : current);
    } catch {
      setError('Could not disconnect Apple Music.');
    } finally {
      setBusy(false);
    }
  };

  const message = !status?.configured
    ? 'Apple Music is not configured for this deployment.'
    : status.requires_reauthorization
      ? 'Apple Music authorization needs to be renewed.'
      : status.linked
        ? `Connected to Apple Music (${status.storefront?.toUpperCase() ?? 'storefront unknown'}).`
        : 'Connect Apple Music to enable account-specific features.';

  return (
    <section
      aria-labelledby="apple-music-account-heading"
      style={{ background: 'var(--card)', borderRadius: '0.75rem', padding: '1.5rem', marginTop: '1.5rem' }}
    >
      <h2 id="apple-music-account-heading" style={{ marginTop: 0, marginBottom: '0.5rem', fontSize: '1.1rem' }}>
        Apple Music
      </h2>
      <p role="status" style={{ color: 'var(--text-secondary)', fontSize: '0.875rem', margin: '0 0 1rem' }}>
        {loading ? 'Loading account status…' : message}
      </p>
      {error && <p role="alert" style={{ color: 'var(--color-danger)', fontSize: '0.875rem' }}>{error}</p>}
      {!loading && status?.linked ? (
        <button className="btn" onClick={disconnect} disabled={busy}>
          {busy ? 'Working…' : 'Disconnect Apple Music'}
        </button>
      ) : (
        <button
          className="btn btn-primary"
          onClick={connect}
          disabled={loading || busy || !status?.configured}
        >
          {busy ? 'Connecting…' : status?.requires_reauthorization ? 'Reconnect Apple Music' : 'Connect Apple Music'}
        </button>
      )}
    </section>
  );
}
