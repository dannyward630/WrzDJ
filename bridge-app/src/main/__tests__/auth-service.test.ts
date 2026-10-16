import { describe, it, expect, vi, beforeEach } from 'vitest';
import { login, verifyToken, buildAuthState } from '../auth-service.js';

const mockFetch = vi.fn();
global.fetch = mockFetch;

beforeEach(() => {
  vi.clearAllMocks();
});

describe('login', () => {
  it('sends form-encoded POST and returns token + username', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ access_token: 'jwt-token-123', token_type: 'bearer' }),
    });

    const result = await login('https://api.wrzdj.com', 'admin', 'pass123');

    expect(result).toEqual({ accessToken: 'jwt-token-123', username: 'admin' });
    expect(mockFetch).toHaveBeenCalledWith(
      'https://api.wrzdj.com/api/auth/login',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      }),
    );

    // Verify form body contains username and password
    const callArgs = mockFetch.mock.calls[0][1];
    const body = callArgs.body as URLSearchParams;
    expect(body.get('username')).toBe('admin');
    expect(body.get('password')).toBe('pass123');
  });

  it('throws on 401 with detail message', async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 401,
      json: () => Promise.resolve({ detail: 'Invalid credentials' }),
    });

    await expect(login('https://api.wrzdj.com', 'admin', 'wrong')).rejects.toThrow('Invalid credentials');
  });

  it('throws generic message when response has no detail', async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 500,
      json: () => Promise.reject(new Error('parse error')),
    });

    await expect(login('https://api.wrzdj.com', 'admin', 'pass')).rejects.toThrow('Login failed');
  });

  // Regression for a3e05cb3 / #820: malformed error bodies need a usable fallback.
  it.each([null, { detail: { message: 'invalid' } }, { detail: '   ' }])(
    'uses the login fallback for malformed error body %j', async (body) => {
      mockFetch.mockResolvedValue({ ok: false, json: () => Promise.resolve(body) });

      await expect(login('https://api.wrzdj.com', 'admin', 'pass')).rejects.toThrow('Login failed');
    },
  );

  // Regression for a3e05cb3 / #820: malformed success bodies must not become stored tokens.
  it.each([
    ['empty object', {}],
    ['null body', null],
    ['missing token', { token_type: 'bearer' }],
    ['non-string token', { access_token: 123 }],
    ['empty token', { access_token: '' }],
    ['whitespace token', { access_token: '   ' }],
  ])('rejects malformed successful response: %s', async (_label, body) => {
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(body) });

    await expect(login('https://api.wrzdj.com', 'admin', 'pass'))
      .rejects.toThrow('Login response did not include a valid access token');
  });

  it('preserves a valid access token exactly', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ access_token: '  jwt-token-123  ' }),
    });

    await expect(login('https://api.wrzdj.com', 'admin', 'pass'))
      .resolves.toEqual({ accessToken: '  jwt-token-123  ', username: 'admin' });
  });
});

describe('verifyToken', () => {
  it('returns username when token is valid', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ username: 'admin' }),
    });

    const result = await verifyToken('https://api.wrzdj.com', 'valid-token');

    expect(result).toBe('admin');
    expect(mockFetch).toHaveBeenCalledWith(
      'https://api.wrzdj.com/api/auth/me',
      { headers: { Authorization: 'Bearer valid-token' } },
    );
  });

  it('returns null when token is expired', async () => {
    mockFetch.mockResolvedValue({ ok: false, status: 401 });

    const result = await verifyToken('https://api.wrzdj.com', 'expired-token');
    expect(result).toBeNull();
  });

  it('returns null on forbidden token', async () => {
    mockFetch.mockResolvedValue({ ok: false, status: 403 });

    await expect(verifyToken('https://api.wrzdj.com', 'forbidden-token')).resolves.toBeNull();
  });

  it('throws on network error', async () => {
    mockFetch.mockRejectedValue(new Error('network error'));

    await expect(verifyToken('https://api.wrzdj.com', 'any-token'))
      .rejects.toThrow('Token verification unavailable');
  });

  it('throws on server and malformed responses', async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 500 });
    await expect(verifyToken('https://api.wrzdj.com', 'server-error'))
      .rejects.toThrow('Token verification failed (500)');

    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve({}) });
    await expect(verifyToken('https://api.wrzdj.com', 'malformed'))
      .rejects.toThrow('invalid username');
  });
});

describe('buildAuthState', () => {
  it('builds authenticated state', () => {
    const state = buildAuthState('https://api.wrzdj.com', 'admin');
    expect(state).toEqual({
      isAuthenticated: true,
      username: 'admin',
      apiUrl: 'https://api.wrzdj.com',
    });
  });

  it('builds unauthenticated state', () => {
    const state = buildAuthState('https://api.wrzdj.com', null);
    expect(state).toEqual({
      isAuthenticated: false,
      username: null,
      apiUrl: 'https://api.wrzdj.com',
    });
  });
});
