#!/usr/bin/env node
/* eslint-disable */
// Verity ships this file into the repository under test, where it is subject to
// whatever linter that repository runs in CI. koa runs `standard`, which found
// 275 style violations here and failed the default branch the moment Verity
// bootstrapped - a red build the user did not cause and could not fix without
// editing a Verity-managed file. This is not the repository's code to hold to
// its own style, so it opts out rather than guessing at a house style Verity
// cannot know in advance.
/**
 * Explore a running application and report what is broken, with evidence.
 *
 * Runs on a GitHub Actions runner inside a checkout of the repository under
 * test. Two things follow from that, and they are the whole reason this is not
 * a Lambda:
 *
 *  - **Time.** A runner allows six hours. Lambda caps at fifteen minutes, which
 *    is not enough to walk a real application.
 *  - **Source.** The repository is on disk. A console error that reports
 *    `at https://app.example.com/assets/checkout-a1b2.js:42` can be resolved to
 *    the file in the repo that produced it, here, with no fetch API and no
 *    round trip. A finding that names a file is one an engineer can act on.
 *
 * ## Privacy
 *
 * `.verity/config.yml` carries `policies.privacy.send_raw_logs_to_verity` and
 * `send_raw_source_to_verity`, both `false` by default, and this script obeys
 * them. The complete console and network logs are written to the workflow's own
 * artifacts, which never leave the customer's GitHub account. What is sent back
 * is the finding, its counts and — when source sharing is on — the repository
 * path it maps to. Never file contents.
 *
 * Secrets are redacted before anything is written, artifact or callback alike.
 *
 * ## What it does not do
 *
 * It does not log in, submit forms, or click anything destructive. Exploration
 * is read-only: it follows links. Write-heavy journeys belong to a test case
 * with a persona and an environment whose isolation someone has confirmed.
 */

import { writeFile, mkdir, readdir, stat } from 'node:fs/promises';
import { join, relative, sep } from 'node:path';
import { chromium } from 'playwright';

const env = (name, fallback = '') => (process.env[name] ?? fallback).toString().trim();
const int = (name, fallback) => {
  const parsed = Number.parseInt(env(name), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
};

const START_URL = env('VERITY_EXPLORE_URL');
const MAX_PAGES = int('VERITY_EXPLORE_MAX_PAGES', 25);
const MAX_DEPTH = int('VERITY_EXPLORE_MAX_DEPTH', 3);
const BUDGET_MS = int('VERITY_EXPLORE_BUDGET_MINUTES', 20) * 60_000;
const PAGE_TIMEOUT_MS = int('VERITY_EXPLORE_PAGE_TIMEOUT_MS', 30_000);
const OUT_DIR = env('VERITY_EXPLORE_OUT', '.verity/exploration');
const RUN_ID = env('VERITY_EXPLORE_RUN_ID', `explore-${Date.now()}`);
const SEND_RAW_LOGS = env('VERITY_SEND_RAW_LOGS') === 'true';
const SEND_SOURCE_PATHS = env('VERITY_SEND_SOURCE_PATHS') === 'true';
const LOGIN_EMAIL = env('VERITY_APP_EMAIL');
const LOGIN_PASSWORD = env('VERITY_APP_PASSWORD');
const LOGIN_URL = env('VERITY_EXPLORE_LOGIN_URL');
/**
 * Whether this environment is known to have its own data store.
 *
 * `false` or unset is not "no" — it is "nobody has said", and the crawl treats
 * the two the same, because the cost of being wrong is a mutation against real
 * customer records. Only an explicit yes widens what a link may be.
 */
const ISOLATED = env('VERITY_ENVIRONMENT_ISOLATED') === 'true';

if (!START_URL) {
  console.error('VERITY_EXPLORE_URL is required. Refusing to guess a target.');
  process.exit(2);
}

/* ------------------------------------------------------------------ redaction */

const SECRET_PATTERN =
  /(api[_-]?key|authorization|bearer|client[_-]?secret|password|private[_-]?key|secret|token)\s*[:=]\s*['"]?[^'"\s,}]+/gi;
const SENSITIVE_HEADERS = new Set([
  'authorization', 'cookie', 'set-cookie', 'proxy-authorization',
  'x-api-key', 'x-auth-token', 'x-csrf-token', 'x-session-token',
]);

const redact = (value) => String(value ?? '').replace(SECRET_PATTERN, (_m, key) => `${key}=<redacted>`);
const redactHeaders = (headers = {}) =>
  Object.fromEntries(
    Object.entries(headers).map(([k, v]) => [k, SENSITIVE_HEADERS.has(k.toLowerCase()) ? '<redacted>' : redact(v)]),
  );
const truncate = (text, max = 4000) =>
  text.length > max ? `${text.slice(0, max)}… <truncated ${text.length - max} chars>` : text;

/* ------------------------------------------------------------- url handling */

const startOrigin = new URL(START_URL).origin;

/** Same page, ignoring the fragment and the ordering of query parameters. */
const canonical = (raw) => {
  const url = new URL(raw);
  url.hash = '';
  url.searchParams.sort();
  return url.toString().replace(/\/$/, '') || url.toString();
};

/** Ending our own session mid-crawl helps nobody. */
const SIGN_OUT_PATH = /(^|\/)(logout|log-out|signout|sign-out|sign_out)(\/|$)/i;

/**
 * Addresses that read like they change something.
 *
 * Deliberately broad and deliberately only consulted when isolation is
 * unconfirmed. A missed page costs coverage; a followed `/delete` costs data.
 */
const MUTATING_PATH =
  /(^|\/|[?&=])(delete|destroy|remove|revoke|cancel|deactivate|disable|archive|purge|reset|drop|terminate|unsubscribe|refund)(\/|$|[?&=])/i;

const isCrawlable = (raw) => {
  let url;
  try {
    url = new URL(raw);
  } catch {
    return false;
  }
  if (url.origin !== startOrigin) return false;
  if (!['http:', 'https:'].includes(url.protocol)) return false;
  // Assets and downloads are fetched by the pages that need them and recorded
  // in the network log; visiting them as pages finds nothing and spends budget.
  if (/\.(png|jpe?g|gif|svg|webp|ico|css|js|mjs|map|woff2?|ttf|eot|pdf|zip|mp4|webm)$/i.test(url.pathname)) {
    return false;
  }
  // Signing out would end the session mid-crawl and turn every later page into
  // a login redirect, so it is skipped whatever the environment is.
  if (SIGN_OUT_PATH.test(url.pathname)) return false;
  // A link is only a read if following it does not change anything. Plenty of
  // applications hang deletion off a GET, and a crawler cannot tell by looking.
  // Unless someone has confirmed this environment has its own data store, an
  // address that reads like a mutation is left alone.
  if (!ISOLATED && MUTATING_PATH.test(url.pathname + url.search)) return false;
  return true;
};

/* ------------------------------------------- mapping a stack frame to a file */

/**
 * Every file in the repository, indexed by basename.
 *
 * Built once. A bundled asset is served as `/assets/checkout-a1b2c3.js`, so the
 * useful signal is the stem before the content hash — `checkout` — matched
 * against source files that could have produced it.
 */
const buildRepoIndex = async (root = process.cwd()) => {
  const skip = new Set(['node_modules', '.git', 'dist', 'build', '.next', 'out', 'coverage', '.verity']);
  const byStem = new Map();

  const walk = async (dir, depth = 0) => {
    if (depth > 8) return;
    let entries;
    try {
      entries = await readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (entry.name.startsWith('.') && entry.name !== '.verity') continue;
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        if (skip.has(entry.name)) continue;
        await walk(full, depth + 1);
      } else if (/\.(ts|tsx|js|jsx|mjs|cjs|vue|svelte)$/.test(entry.name)) {
        const stem = entry.name.replace(/\.[^.]+$/, '');
        if (!byStem.has(stem)) byStem.set(stem, []);
        byStem.get(stem).push(relative(root, full).split(sep).join('/'));
      }
    }
  };

  await walk(root);
  return byStem;
};

/**
 * Best-effort repository path for a browser URL.
 *
 * Deliberately conservative: it returns a path only when exactly one source
 * file could plausibly have produced the asset. An ambiguous guess attached to
 * a finding is worse than no path at all — it sends an engineer to the wrong
 * file with false confidence.
 */
const mapToRepoPath = (assetUrl, repoIndex) => {
  if (!assetUrl) return null;
  let pathname;
  try {
    pathname = new URL(assetUrl).pathname;
  } catch {
    return null;
  }
  const base = pathname.split('/').pop() || '';
  const stem = base
    .replace(/\.[^.]+$/, '')
    .replace(/[.-][a-f0-9]{6,}$/i, '')  // vite/webpack content hash
    .replace(/\.min$/, '');
  if (!stem) return null;
  const matches = repoIndex.get(stem);
  return matches && matches.length === 1 ? matches[0] : null;
};


/* ------------------------------------------------------------------ sign-in */

/**
 * Does this page look like it is still asking us to sign in?
 *
 * The check that makes the difference between a crawl and a lie. A run that
 * walks a login wall and reports nothing broken has tested nothing and said
 * everything is fine.
 */
const looksLikeLogin = async (page) => {
  try {
    const passwordField = await page.$('input[type="password"]');
    if (passwordField) return true;
    const url = page.url().toLowerCase();
    return /\/(login|signin|sign-in|auth)(\/|\?|$)/.test(url);
  } catch {
    return false;
  }
};

/** The first selector in the list that matches something visible. */
const firstVisibleSelector = async (page, selectors) => {
  for (const selector of selectors) {
    try {
      const handle = await page.$(selector);
      if (handle && (await handle.isVisible())) return selector;
    } catch {
      /* a selector this page does not understand is not an error */
    }
  }
  return null;
};

/** First match wins; each list is ordered most-specific first. */
const firstVisible = async (page, selectors) => {
  for (const selector of selectors) {
    try {
      const handle = await page.$(selector);
      if (handle && (await handle.isVisible())) return handle;
    } catch {
      /* a selector this page does not understand is not an error */
    }
  }
  return null;
};

const EMAIL_SELECTORS = [
  'input[type="email"]',
  'input[name="email"]',
  'input[id="email"]',
  'input[autocomplete="username"]',
  'input[name="username"]',
  'input[type="text"][name*="mail" i]',
];
const PASSWORD_SELECTORS = ['input[type="password"]', 'input[name="password"]', 'input[id="password"]'];
const SUBMIT_SELECTORS = [
  'button[type="submit"]',
  'input[type="submit"]',
  'button:has-text("Sign in")',
  'button:has-text("Log in")',
  'button:has-text("Login")',
  'button:has-text("Continue")',
];

/**
 * Sign in, and say honestly whether it worked.
 *
 * Returns one of:
 *   `skipped`  - no credentials were provided; crawl anonymously
 *   `authed`   - the form was submitted and the app stopped asking
 *   `failed`   - credentials were provided and we are still at a login form
 *
 * `failed` is not a finding about the application. It is a statement that this
 * run could not see the thing it was sent to look at, and the caller stops.
 */
const signIn = async (page) => {
  if (!LOGIN_EMAIL || !LOGIN_PASSWORD) return { state: 'skipped' };

  const loginUrl = LOGIN_URL || new URL('/login', START_URL).toString();
  console.log(`[verity-explore] signing in as ${LOGIN_EMAIL.replace(/(.).*(@.*)/, '$1***$2')}`);

  try {
    await page.goto(loginUrl, { waitUntil: 'domcontentloaded', timeout: PAGE_TIMEOUT_MS });
    await page.waitForLoadState('networkidle', { timeout: 10_000 }).catch(() => {});

    /*
     * Wait for the password field before touching anything.
     *
     * These are single-page applications: the inputs exist in the DOM before
     * the framework has attached to them, so a query that returns immediately
     * can fill a field whose state nothing is listening to, and the form
     * submits empty. Waiting for it to be actionable is what makes the fill
     * count.
     */
    const passwordSelector = await (async () => {
      for (const selector of PASSWORD_SELECTORS) {
        try {
          await page.waitForSelector(selector, { state: 'visible', timeout: 8000 });
          return selector;
        } catch {
          /* try the next shape of login form */
        }
      }
      return null;
    })();
    const emailSelector = EMAIL_SELECTORS.find(Boolean) && (await firstVisibleSelector(page, EMAIL_SELECTORS));

    if (!emailSelector || !passwordSelector) {
      return { state: 'failed', reason: `No sign-in form was found at ${loginUrl}.` };
    }

    await page.fill(emailSelector, LOGIN_EMAIL);
    await page.fill(passwordSelector, LOGIN_PASSWORD);

    const submitSelector = await firstVisibleSelector(page, SUBMIT_SELECTORS);
    if (submitSelector) await page.click(submitSelector);
    else await page.press(passwordSelector, 'Enter');

    // Give the app its redirect, then ask whether it actually let us in.
    await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
    await page.waitForTimeout(1500);

    if (await looksLikeLogin(page)) {
      /*
       * Say what was actually seen. "Still showed a sign-in form" is true and
       * useless: it does not distinguish a rejected password from a form that
       * never received the keystrokes, and those have opposite fixes.
       */
      const stillHasPassword = Boolean(await page.$('input[type="password"]').catch(() => null));
      const visibleError = await page
        .evaluate(() => {
          const candidates = Array.from(
            document.querySelectorAll('[role="alert"], .error, [class*="error" i], [data-error]'),
          );
          const text = candidates.map((el) => (el.textContent || '').trim()).find((t) => t.length > 0 && t.length < 200);
          return text || null;
        })
        .catch(() => null);

      return {
        state: 'failed',
        reason:
          `After submitting, the page was at ${page.url()}` +
          (stillHasPassword ? ' and still showed a password field' : '') +
          (visibleError ? `. The page said: ${redact(visibleError)}` : '.'),
      };
    }
    console.log(`[verity-explore] signed in, now at ${page.url()}`);
    return { state: 'authed' };
  } catch (error) {
    return { state: 'failed', reason: redact(error?.message ?? String(error)) };
  }
};


/* --------------------------------------------------- navigating by clicking */

/**
 * Modern applications do not navigate with links.
 *
 * SignalFoundry's workspace picker — the first page behind its login — renders
 * each workspace as a `<button>`, with `role="link"` on nothing and `href` on
 * nothing. A crawler that follows only `<a href>` sees the shell of an
 * application and reports two pages, which is why signing in was not on its own
 * enough to explore anything.
 *
 * So: click things that look like navigation, keep whatever address that
 * produces, and go back. A click that does not change the address is discarded
 * — it opened a menu or a modal, and this is not the tool for those.
 */

/** Never click these, whatever the environment. Their names say what they do. */
const DESTRUCTIVE_LABEL =
  /\b(delete|remove|destroy|revoke|cancel|deactivate|disable|archive|purge|reset|drop|terminate|unsubscribe|refund|sign\s?out|log\s?out)\b/i;

/** Clicking blind is how a crawler files an empty form or sends an invite. */
const SUBMIT_LABEL = /\b(submit|send|save|create|add|invite|pay|confirm|publish|deploy|buy|subscribe)\b/i;

const CLICKABLE_SELECTOR =
  'button, [role="button"], [role="link"], [data-testid], [class*="cursor-pointer" i]';

/** How many candidates one page may spend. Each costs a navigation and a back. */
const MAX_CLICKS_PER_PAGE = 8;

/**
 * Addresses reachable from this page by clicking rather than by link.
 *
 * Bounded, reversible, and conservative about what it will touch: a label that
 * reads like a deletion or a form submission is left alone even when the
 * environment is isolated, because "isolated" licenses reading real data, not
 * writing to it.
 */
const discoverByClicking = async (page, target, known) => {
  const found = [];
  let candidates = [];
  try {
    candidates = await page.evaluate(
      ({ selector, max }) => {
        const seen = new Set();
        const out = [];
        for (const el of Array.from(document.querySelectorAll(selector))) {
          if (out.length >= max) break;
          const rect = el.getBoundingClientRect();
          if (rect.width < 8 || rect.height < 8) continue;
          const label = (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 80);
          if (!label || seen.has(label)) continue;
          seen.add(label);
          out.push(label);
        }
        return out;
      },
      { selector: CLICKABLE_SELECTOR, max: MAX_CLICKS_PER_PAGE * 3 },
    );
  } catch {
    return found;
  }

  let spent = 0;
  for (const label of candidates) {
    if (spent >= MAX_CLICKS_PER_PAGE) break;
    if (DESTRUCTIVE_LABEL.test(label) || SUBMIT_LABEL.test(label)) continue;

    try {
      const element = page.locator(`${CLICKABLE_SELECTOR}`, { hasText: label }).first();
      if (!(await element.isVisible({ timeout: 1000 }).catch(() => false))) continue;

      spent += 1;
      await element.click({ timeout: 5000, noWaitAfter: true });
      await page.waitForLoadState('networkidle', { timeout: 6000 }).catch(() => {});
      await page.waitForTimeout(600);

      const now = page.url();
      if (isCrawlable(now) && canonical(now) !== canonical(target)) {
        const address = canonical(now);
        if (!known.has(address) && !found.includes(address)) {
          found.push(address);
          // The address only. A control's label is the customer's own words —
          // workspace names, people's names — and belongs in the report, which
          // stays in their repository, not in a log line.
          console.log(`[verity-explore]   found by clicking: ${address}`);
        }
      }

      // Back to where we were, whatever happened, so the next candidate is
      // clicked from the page this function was asked about.
      if (canonical(page.url()) !== canonical(target)) {
        await page.goto(target, { waitUntil: 'domcontentloaded', timeout: PAGE_TIMEOUT_MS }).catch(() => {});
        await page.waitForLoadState('networkidle', { timeout: 6000 }).catch(() => {});
      }
    } catch {
      /* one uncooperative control must not end the page's exploration */
    }
  }
  return found;
};

/* ------------------------------------------------------------------- crawl */

const startedAt = Date.now();
const remaining = () => BUDGET_MS - (Date.now() - startedAt);

const visited = new Set();
const pages = [];
const findings = [];
const consoleLog = [];
const networkLog = [];

const addFinding = (finding) => {
  findings.push({ id: `F-${findings.length + 1}`, ...finding });
};

const explorePage = async (context, target, depth, repoIndex) => {
  const page = await context.newPage();
  const pageConsole = [];
  const pageNetwork = [];

  page.on('console', (message) => {
    try {
      const location = message.location?.() ?? {};
      pageConsole.push({
        level: message.type(),
        text: truncate(redact(message.text())),
        location: location.url ? { url: location.url, line: location.lineNumber, column: location.columnNumber } : undefined,
        timestamp: Date.now(),
      });
    } catch { /* one bad message must not stop the page */ }
  });

  page.on('pageerror', (error) => {
    pageConsole.push({
      level: 'pageerror',
      text: truncate(redact(error?.message ?? String(error))),
      stack: truncate(redact(error?.stack ?? ''), 2000),
      timestamp: Date.now(),
    });
  });

  page.on('response', (response) => {
    try {
      pageNetwork.push({
        url: response.url(),
        method: response.request().method(),
        status: response.status(),
        resourceType: response.request().resourceType(),
        responseHeaders: redactHeaders(response.headers()),
        timestamp: Date.now(),
      });
    } catch { /* as above */ }
  });

  page.on('requestfailed', (request) => {
    try {
      pageNetwork.push({
        url: request.url(),
        method: request.method(),
        resourceType: request.resourceType(),
        failure: redact(request.failure()?.errorText ?? 'Request failed'),
        timestamp: Date.now(),
      });
    } catch { /* as above */ }
  });

  const record = { url: target, depth, visitedAt: Date.now() };

  try {
    const response = await page.goto(target, { waitUntil: 'domcontentloaded', timeout: PAGE_TIMEOUT_MS });
    record.status = response?.status() ?? null;
    await page.waitForLoadState('networkidle', { timeout: 10_000 }).catch(() => {});

    record.title = await page.title().catch(() => '');

    const shotPath = join(OUT_DIR, 'screenshots', `${encodeURIComponent(canonical(target)).slice(-120)}.png`);
    await page.screenshot({ path: shotPath, fullPage: false, timeout: 15_000 }).catch(() => {});
    record.screenshot = relative(OUT_DIR, shotPath).split(sep).join('/');

    // A page that renders nothing is broken in a way no console error reports.
    const textLength = await page.evaluate(() => (document.body?.innerText ?? '').trim().length).catch(() => 0);
    record.textLength = textLength;

    if (record.status && record.status >= 400) {
      addFinding({
        kind: 'http-error',
        severity: record.status >= 500 ? 'critical' : 'high',
        title: `Page returned HTTP ${record.status}`,
        pageUrl: target,
        evidence: { status: record.status, screenshot: record.screenshot },
      });
    } else if (textLength < 20) {
      addFinding({
        kind: 'blank-page',
        severity: 'high',
        title: 'Page rendered with almost no visible text',
        pageUrl: target,
        evidence: { textLength, screenshot: record.screenshot },
      });
    }

    const links = await page
      .evaluate(() => Array.from(document.querySelectorAll('a[href]'), (a) => a.href))
      .catch(() => []);
    record.linkCount = links.length;
    record.links = links.filter(isCrawlable).map(canonical);

    /*
     * Links alone are not the site. Only spend clicks while there is still
     * depth left to descend into — discovering an address the crawl will never
     * visit costs a navigation and buys nothing.
     */
    if (depth < MAX_DEPTH) {
      const clicked = await discoverByClicking(page, target, new Set([...visited, ...record.links]));
      record.clickDiscovered = clicked.length;
      record.links = [...new Set([...record.links, ...clicked])];
    }
  } catch (error) {
    record.error = redact(error?.message ?? String(error));
    addFinding({
      kind: 'navigation-failed',
      severity: 'critical',
      title: `Could not load the page: ${record.error}`,
      pageUrl: target,
      evidence: {},
    });
  }

  // Console errors become findings; chatter stays in the log.
  for (const entry of pageConsole) {
    if (!['error', 'warning', 'pageerror'].includes(entry.level)) continue;
    const repoPath = SEND_SOURCE_PATHS ? mapToRepoPath(entry.location?.url, repoIndex) : null;
    addFinding({
      kind: entry.level === 'pageerror' ? 'uncaught-exception' : `console-${entry.level}`,
      severity: entry.level === 'warning' ? 'low' : 'high',
      title: truncate(entry.text, 200),
      pageUrl: target,
      ...(repoPath ? { relatedFiles: [repoPath] } : {}),
      evidence: {
        screenshot: record.screenshot,
        ...(entry.location ? { sourceLocation: entry.location } : {}),
        ...(SEND_RAW_LOGS ? { text: entry.text } : {}),
      },
    });
  }

  for (const entry of pageNetwork) {
    const broken = entry.failure || (entry.status && entry.status >= 400);
    if (!broken) continue;
    // A page's own 4xx is already reported above as `http-error`.
    if (!entry.failure && canonical(entry.url) === canonical(target)) continue;
    addFinding({
      kind: entry.failure ? 'request-failed' : 'api-error',
      severity: entry.failure || entry.status >= 500 ? 'high' : 'medium',
      title: entry.failure
        ? `${entry.method} ${entry.url} never completed: ${entry.failure}`
        : `${entry.method} ${entry.url} returned ${entry.status}`,
      pageUrl: target,
      evidence: {
        screenshot: record.screenshot,
        request: { url: entry.url, method: entry.method, status: entry.status ?? null, failure: entry.failure ?? null },
      },
    });
  }

  consoleLog.push({ pageUrl: target, entries: pageConsole });
  networkLog.push({ pageUrl: target, entries: pageNetwork });
  pages.push(record);

  await page.close().catch(() => {});
  return record.links ?? [];
};

const main = async () => {
  await mkdir(join(OUT_DIR, 'screenshots'), { recursive: true });

  console.log(`[verity-explore] ${START_URL} — up to ${MAX_PAGES} pages, depth ${MAX_DEPTH}, ${BUDGET_MS / 60000} min`);

  const repoIndex = SEND_SOURCE_PATHS ? await buildRepoIndex() : new Map();
  if (SEND_SOURCE_PATHS) console.log(`[verity-explore] indexed ${repoIndex.size} source stems for stack mapping`);

  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    ignoreHTTPSErrors: true,
  });

  /*
   * Sign in first, on a page of its own, so the session cookie is on the
   * context before the crawl starts and every page below is fetched as the
   * signed-in user.
   */
  const authPage = await context.newPage();
  const auth = await signIn(authPage);
  await authPage.close().catch(() => {});

  if (auth.state === 'failed') {
    /*
     * Stop, and say so. Crawling on from here would walk a login wall and
     * report "nothing broken" about pages it never saw — `PRODUCT.md` requires
     * a failed auth to create no issues, and this is why.
     */
    // The reason can quote the page, so it travels in the report rather than
    // in stdout. `authFailureReason` below carries it to whoever needs it.
    console.error('[verity-explore] sign-in failed; see authFailureReason in the report');
    await context.close().catch(() => {});
    await browser.close().catch(() => {});

    const report = {
      runId: RUN_ID,
      startUrl: START_URL,
      startedAt,
      finishedAt: Date.now(),
      durationMs: Date.now() - startedAt,
      stopReason: 'auth-failed',
      authState: 'failed',
      authFailureReason: auth.reason,
      pagesVisited: 0,
      pagesQueued: 0,
      findingCount: 0,
      bySeverity: {},
      pages: [],
      findings: [],
    };
    await writeFile(join(OUT_DIR, 'report.json'), JSON.stringify(report, null, 2));
    console.log('[verity-explore] done: sign-in failed, nothing explored and nothing claimed');
    process.exit(0);
  }

  if (auth.state === 'authed') {
    console.log('[verity-explore] exploring as a signed-in user');
  } else {
    console.log('[verity-explore] no credentials provided; exploring anonymously');
  }

  /** Breadth-first: the pages one click from the entry point matter more than
   *  a deep tail, and a budget that runs out should have spent itself near the
   *  top of the site rather than in one corner of it. */
  const frontier = [{ url: canonical(START_URL), depth: 0 }];
  let stopReason = 'completed';

  while (frontier.length) {
    if (pages.length >= MAX_PAGES) { stopReason = 'page-limit'; break; }
    if (remaining() <= PAGE_TIMEOUT_MS) { stopReason = 'budget-exhausted'; break; }

    const next = frontier.shift();
    if (visited.has(next.url)) continue;
    visited.add(next.url);

    console.log(`[verity-explore] (${pages.length + 1}/${MAX_PAGES}) depth ${next.depth}: ${next.url}`);
    const links = await explorePage(context, next.url, next.depth, repoIndex);

    if (next.depth < MAX_DEPTH) {
      for (const link of links) {
        if (!visited.has(link) && !frontier.some((item) => item.url === link)) {
          frontier.push({ url: link, depth: next.depth + 1 });
        }
      }
    }
  }

  await context.close().catch(() => {});
  await browser.close().catch(() => {});

  const bySeverity = findings.reduce((acc, f) => ({ ...acc, [f.severity]: (acc[f.severity] ?? 0) + 1 }), {});
  const report = {
    runId: RUN_ID,
    startUrl: START_URL,
    startedAt,
    finishedAt: Date.now(),
    durationMs: Date.now() - startedAt,
    stopReason,
    // Which of the app a reader is looking at. A crawl of the signed-out
    // surface and a crawl of the product are not the same evidence, and the
    // report should never leave that ambiguous.
    authState: auth.state,
    pagesVisited: pages.length,
    pagesQueued: frontier.length,
    findingCount: findings.length,
    bySeverity,
    pages,
    findings,
  };

  await writeFile(join(OUT_DIR, 'report.json'), JSON.stringify(report, null, 2));
  // Full logs stay in the workflow's own artifacts unless the project opts in.
  await writeFile(join(OUT_DIR, 'console.json'), JSON.stringify(consoleLog, null, 2));
  await writeFile(join(OUT_DIR, 'network.json'), JSON.stringify(networkLog, null, 2));

  // Counts, formatted by hand rather than serialised. A stringified object in a
  // log line is how page content leaks into logs, and the repo's
  // `noRawAiContentLogging` rule is right to forbid the shape outright.
  const severitySummary = Object.entries(bySeverity)
    .map(([level, count]) => `${level}=${count}`)
    .join(' ') || 'none';
  console.log(
    `[verity-explore] done: pages=${pages.length} findings=${findings.length} ` +
    `${severitySummary} stop=${stopReason}`,
  );

  // Findings are the product of this run, not a failure of it. Exiting non-zero
  // for a site with bugs would make the workflow red for doing its job, and the
  // callback is what reports them.
  process.exit(0);
};

main().catch((error) => {
  console.error('[verity-explore] fatal:', redact(error?.stack ?? error?.message ?? error));
  process.exit(1);
});
