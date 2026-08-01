export function buildAuthCallbackUrl(
  origin = window.location.origin,
  baseUrl = import.meta.env.BASE_URL,
): string {
  const normalizedBase = baseUrl.endsWith("/") ? baseUrl : `${baseUrl}/`;
  return new URL(`${normalizedBase}auth/callback`, origin).toString();
}

export function buildCurrentCallbackUrl(
  search: string,
  hash: string,
  origin = window.location.origin,
  baseUrl = import.meta.env.BASE_URL,
): URL {
  const callback = new URL(buildAuthCallbackUrl(origin, baseUrl));
  callback.search = search;
  callback.hash = hash;
  return callback;
}
