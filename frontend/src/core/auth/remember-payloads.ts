export function buildLocalLoginBody(input: {
  email: string;
  password: string;
  rememberMe: boolean;
}): string {
  return new URLSearchParams({
    username: input.email,
    password: input.password,
    remember_me: String(input.rememberMe),
  }).toString();
}

export function buildRememberingCredentialPayload(input: {
  email: string;
  password: string;
  rememberMe: boolean;
}) {
  return {
    email: input.email,
    password: input.password,
    remember_me: input.rememberMe,
  };
}

export function buildOidcLoginUrl(input: {
  next: string;
  providerId: string;
  rememberMe: boolean;
}): string {
  const query = new URLSearchParams({
    next: input.next,
    remember_me: String(input.rememberMe),
  });
  return `/api/v1/auth/oauth/${encodeURIComponent(input.providerId)}?${query.toString()}`;
}

export function buildSetupPasswordChangePayload(input: {
  currentPassword: string;
  email: string;
  newPassword: string;
  rememberMe: boolean;
}) {
  return {
    current_password: input.currentPassword,
    new_password: input.newPassword,
    new_email: input.email || undefined,
    remember_me: input.rememberMe,
  };
}
