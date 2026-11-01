/**
 * leadbot v2 → Google Sheets bridge  (TWO tabs)
 * ------------------------------------------------------------------------
 * WHY THIS EXISTS
 * leadbot runs on GitHub's servers and has no Google credentials. Rather than
 * put a Google service-account JSON in a repo (a real secret-leak risk on a
 * phone-managed project), we let Apps Script — which already runs inside
 * Google — do the write. leadbot POSTs JSON, this appends rows.
 *
 * v2 adds a SECOND tab, "Local Business Leads", written by localbot.py.
 * Both pipelines post to the same endpoint; the payload's `tab` field decides
 * which sheet receives the row.
 *
 * SETUP (all from a phone browser, ~6 minutes)
 * 1. Create a Google Sheet. Name it "kk leads".
 * 2. Extensions → Apps Script. Delete the stub, paste this whole file.
 * 3. Change TOKEN below to a long random string. Remember it.
 * 4. Deploy → New deployment → type "Web app"
 *      Execute as:  Me
 *      Who has access:  Anyone
 *    (Deploy as "Anyone", not "Anyone with Google account" — GitHub's servers
 *     are not signed in to Google. The TOKEN is what actually protects this.)
 * 5. Copy the /exec URL. In GitHub → Settings → Secrets and variables →
 *    Actions, add:
 *      SHEETS_WEBHOOK_URL = that /exec URL
 *      SHEETS_TOKEN       = the same TOKEN string
 * 6. In the Apps Script editor, run setup() once and authorise it.
 *
 * NOTE ON v2 UPGRADE: if you already deployed v1, you must Deploy → Manage
 * deployments → Edit → Version: New version. Publishing a new version is what
 * makes GitHub's existing /exec URL serve THIS code. Forgetting this is the
 * single most common "my changes did nothing" cause.
 */

// ── change this ──────────────────────────────────────────────────────────────
const TOKEN = 'CHANGE-ME-to-something-long-and-random';

const TABS = {
  leads: {
    name: 'leads',
    headers: ['date', 'score', 'title', 'company', 'source', 'age',
              'primary', 'matched', 'url', 'status', 'notes'],
    urlCol: 9,      // 1-based column holding the dedupe URL
    widths: { 3: 320, 9: 260 },
  },
  local: {
    name: 'Local Business Leads',
    headers: ['date', 'score', 'business', 'category', 'city', 'phone',
              'email', 'website', 'website status', 'detected problem',
              'social', 'pitch', 'status', 'notes'],
    urlCol: 8,      // dedupe on the website field; blank = no site, so we
                    // fall back to business+city in code below
    widths: { 3: 220, 10: 300, 12: 380 },
  },
};

function setup() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  Object.keys(TABS).forEach(k => {
    const spec = TABS[k];
    let sh = ss.getSheetByName(spec.name);
    if (!sh) sh = ss.insertSheet(spec.name);
    if (sh.getLastRow() === 0) {
      sh.appendRow(spec.headers);
      sh.getRange(1, 1, 1, spec.headers.length)
        .setFontWeight('bold')
        .setBackground('#1A1614')
        .setFontColor('#D4AF37');
      sh.setFrozenRows(1);
      Object.keys(spec.widths).forEach(c =>
        sh.setColumnWidth(Number(c), spec.widths[c]));
      sh.getRange(1, 1, 1, spec.headers.length).createFilter();
    }
  });
  return 'ok';
}

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);

    // Constant-ish time compare is overkill here; the token is high-entropy
    // and this endpoint only appends to kk's own sheet.
    if (!body.token || body.token !== TOKEN) {
      return json({ ok: false, error: 'bad token' });
    }

    const tabKey = (body.tab === 'local') ? 'local' : 'leads';
    const spec = TABS[tabKey];

    const ss = SpreadsheetApp.getActiveSpreadsheet();
    let sh = ss.getSheetByName(spec.name);
    if (!sh) { setup(); sh = ss.getSheetByName(spec.name); }

    // ── de-duplicate so a re-run never doubles a sheet ──
    const existing = new Set();
    const last = sh.getLastRow();
    if (last > 1) {
      const col = tabKey === 'local' ? spec.urlCol : spec.urlCol;
      sh.getRange(2, col, last - 1, 1).getValues()
        .forEach(r => { if (r[0]) existing.add(String(r[0])); });
    }

    const now = new Date();
    let added = 0;
    const rows = body.rows || [];

    rows.forEach(r => {
      let dedupeKey;
      if (tabKey === 'local') {
        // A business with no website has nothing in the website column, so
        // dedupe on name+city instead — otherwise every no-site lead would
        // re-append on every run.
        dedupeKey = String(r.website || '') ||
                    (String(r.business || '') + '|' + String(r.city || ''));
      } else {
        dedupeKey = String(r.url || '');
      }
      if (existing.has(dedupeKey)) return;
      existing.add(dedupeKey);

      if (tabKey === 'local') {
        sh.appendRow([
          now, r.score || '', r.business || '', r.category || '', r.city || '',
          r.phone || '', r.email || '', r.website || '', r.web_status || '',
          r.problem || '', r.social || '', r.pitch || '', 'new', '',
        ]);
      } else {
        sh.appendRow([
          now, r.score || '', r.title || '', r.company || '', r.source || '',
          r.age || '', r.primary || '', r.matched || '', r.url || '',
          'new', '',
        ]);
      }
      added++;
    });

    // ── colour-code by score so the sheet is scannable on a phone ──
    if (sh.getLastRow() > 1) {
      const range = sh.getRange(2, 1, sh.getLastRow() - 1, spec.headers.length);
      const hi = tabKey === 'local' ? 22 : 18;
      sh.setConditionalFormatRules([
        SpreadsheetApp.newConditionalFormatRule()
          .whenNumberGreaterThanOrEqualTo(hi)
          .setBackground('#E8F0E3').setRanges([range]).build(),
        SpreadsheetApp.newConditionalFormatRule()
          .whenNumberLessThan(10)
          .setBackground('#F5EFE9').setRanges([range]).build(),
      ]);
    }

    return json({ ok: true, tab: spec.name, added, duplicates: rows.length - added });
  } catch (err) {
    return json({ ok: false, error: String(err) });
  }
}

// Lets you sanity-check from a browser: visit the /exec URL as GET
function doGet() {
  return json({ ok: true, service: 'leadbot sheets bridge v2',
                tabs: Object.keys(TABS), time: new Date() });
}

function json(o) {
  return ContentService.createTextOutput(JSON.stringify(o))
    .setMimeType(ContentService.MimeType.JSON);
}

/**
 * Phone-friendly status menu. Writes to the STATUS column only, so it never
 * touches the data leadbot wrote.
 */
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('leadbot')
    .addItem('Mark selected → contacted', 'markContacted')
    .addItem('Mark selected → replied', 'markReplied')
    .addItem('Mark selected → won', 'markWon')
    .addItem('Mark selected → dead', 'markDead')
    .addToUi();
}

function markContacted() { setStatus('contacted'); }
function markReplied()   { setStatus('replied'); }
function markWon()       { setStatus('won'); }
function markDead()      { setStatus('dead'); }

function setStatus(s) {
  const sh = SpreadsheetApp.getActiveSheet();
  const name = sh.getName();
  // status column differs per tab: 10 on 'leads', 13 on 'Local Business Leads'
  const col = (name === TABS.local.name)
    ? TABS.local.headers.indexOf('status') + 1
    : TABS.leads.headers.indexOf('status') + 1;
  const r = sh.getActiveRange();
  sh.getRange(r.getRow(), col, r.getNumRows(), 1).setValue(s);
}
