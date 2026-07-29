/**
 * Type-level test: MailProvider must include 'cloudmail'.
 * Run via: npm run typecheck
 */
import type { MailProvider } from './settings';

// Fails typecheck until 'cloudmail' is added to MailProvider union
const _cloudmailIsValidProvider: MailProvider = 'cloudmail';

// Exhaustiveness helper — keep list in sync with MailProvider
const ALL_MAIL_PROVIDERS = [
  'cloudflare',
  'duckmail',
  'yyds',
  'gptmail',
  'cloudmail'
] as const satisfies readonly MailProvider[];

void _cloudmailIsValidProvider;
void ALL_MAIL_PROVIDERS;
