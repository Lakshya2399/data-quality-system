'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { MockBackend, clone } = require('./mock-backend');
const { loadUi, waitFor, sleep } = require('./helpers');

async function open(t, backend, options) {
  const ui = loadUi(backend, options);
  t.after(async () => { await ui.idle(); ui.close(); });     // never tear the page down mid-request
  return ui;
}
const reportReady = (ui) => waitFor(() => ui.$('#report-body .result'), { message: 'the report to render' });

// ----------------------------------------------------------------------------------------------
test('renders health, run list, model, rules and report from real API payloads', async (t) => {
  const backend = new MockBackend();
  const ui = await open(t, backend);
  await reportReady(ui);
  const { fx } = backend;

  assert.ok(ui.text('#backend-status').startsWith('Backend OK'));
  assert.ok(ui.text('#backend-status').includes(fx.health.model));
  assert.ok(ui.$('#backend-status').classList.contains('ok'));

  assert.equal(ui.$$('#run-select option').length, 3);
  assert.equal(ui.$('#run-select').value, backend.goodRunId, 'the newest run is selected automatically');
  assert.ok(ui.window.location.hash.includes(`run=${backend.goodRunId}`));
  assert.match(ui.text('#run-status'), /completed · 8 rules/);

  // data model
  assert.equal(ui.$$('#model-body table').length, 2);
  assert.ok(ui.text('#relationships').includes('sample_orders.region_code → valid_regions.region_code'));
  assert.ok(ui.$$('#constraints li').length >= 1);
  assert.ok(ui.text('#model-body details pre').includes('"tables"'));
  assert.ok(ui.text('#model-body').includes('MIXED') === false, 'the clean sample has a single date format');

  // rules
  assert.equal(ui.$$('#rules-table tbody tr').length, 8);
  assert.equal(ui.$$('#rules-table tr.executes').length, fx.rules.executable_count);
  assert.ok(ui.text('#rules-summary').includes('6 of 8 rules'));
  assert.equal(ui.$$('#rules-table .score-badge')[0].textContent, '5');
  assert.ok(ui.$('#rules-table .score-badge').classList.contains('score-5'));
  assert.ok(ui.text('#rules-table').includes(fx.rules.rules[0].justification));
  assert.equal(ui.text('#execute-hint'), '6 rules rated 4 or higher will run.');

  // report
  const results = ui.$$('#report-body .result');
  assert.equal(results.length, fx.execution.results.length);
  assert.ok(ui.text('#exec-header').includes(`Execution #${fx.execution.exec_id}`));
  assert.ok(results[0].textContent.includes('discount_not_above_amount'));
  assert.ok(results[0].textContent.includes('22 failed'));
  assert.equal(results[0].querySelectorAll('.chip').length, 22);
  assert.ok(parseFloat(results[0].querySelector('.bar-fail').style.width) > 0);
  assert.equal(ui.$$('.summary .stat b').map((b) => b.textContent).join(','), '6,6,0');
  assert.deepEqual(ui.errors, []);
});

test('an unreachable backend shows a clear message instead of crashing', async (t) => {
  const ui = await open(t, new MockBackend({ down: true }));
  await waitFor(() => !ui.$('#banner').hidden, { message: 'the error banner' });
  assert.equal(ui.text('#backend-status'), 'Backend unreachable');
  assert.ok(ui.$('#backend-status').classList.contains('bad'));
  assert.ok(ui.text('#banner').includes('uvicorn app.main:app'));
  assert.ok(ui.text('#run-select option').includes('no runs yet'));
  assert.equal(ui.$('#run-execute').disabled, true);
  assert.deepEqual(ui.errors, []);
});

test('a missing Gemini key is flagged in the header', async (t) => {
  const ui = await open(t, new MockBackend({ geminiConfigured: false }));
  await waitFor(() => ui.text('#backend-status').includes('key missing'));
  assert.ok(ui.$('#backend-status').classList.contains('bad'));
});

test('an empty system shows helpful placeholders and disables execution', async (t) => {
  const ui = await open(t, new MockBackend({ empty: true }));
  await waitFor(() => ui.text('#backend-status').startsWith('Backend OK'));
  await waitFor(() => ui.text('#run-select').includes('no runs yet'));
  assert.equal(ui.text('#run-status'), 'No run selected.');
  assert.ok(ui.text('#model-body').includes('Run the sample pipeline'));
  assert.ok(ui.text('#report-body').includes('Run the sample pipeline first'));
  assert.equal(ui.$('#run-execute').disabled, true);
  assert.equal(ui.text('#execute-hint'), 'Run (or select) a completed sample pipeline first.');
});

// ----------------------------------------------------------------------------------------------
test('starting the pipeline with default files polls created -> running -> completed', async (t) => {
  const backend = new MockBackend();
  const ui = await open(t, backend);
  await reportReady(ui);

  const seen = [];
  new ui.window.MutationObserver(() => {
    const cls = ui.$('#run-status').className;
    if (seen.at(-1) !== cls) seen.push(cls);
  }).observe(ui.$('#run-status'), { attributes: true, attributeFilter: ['class'] });

  ui.submit(ui.$('#pipeline-form'));
  await waitFor(() => ui.text('#run-status').includes('newrun000001 completed'), { message: 'the new run to complete' });
  await waitFor(() => ui.$$('#rules-table tbody tr').length === 8);

  const order = ['status created', 'status running', 'status completed'].map((c) => seen.indexOf(c));
  assert.ok(order.every((i) => i >= 0) && order[0] < order[1] && order[1] < order[2], `status classes seen: ${seen.join(' | ')}`);

  const post = backend.calls.find((c) => c.method === 'POST' && c.path === '/pipeline/run');
  assert.equal(post.body, undefined, 'default mode sends no files');
  assert.ok(backend.calls.filter((c) => c.path === '/runs/newrun000001').length >= 3);
  assert.equal(ui.$('#run-select').value, 'newrun000001');
  assert.equal(ui.$$('#run-select option').length, 4);
  assert.ok(ui.window.location.hash.includes('run=newrun000001'));
  assert.equal(ui.$('#run-pipeline').disabled, false);
  assert.equal(ui.text('#run-pipeline'), 'Run sample pipeline');
  assert.equal(ui.$('#run-execute').disabled, false);
  assert.ok(ui.text('#report-body').includes('Execute the rules on a production file'));
  assert.deepEqual(ui.errors, []);
});

test('upload mode requires both files and sends them as multipart fields', async (t) => {
  const backend = new MockBackend({ empty: true });
  const ui = await open(t, backend);
  await waitFor(() => ui.text('#backend-status').startsWith('Backend OK'));

  assert.equal(ui.$('#pipeline-uploads').hidden, true);
  ui.$('#pipeline-mode-upload').checked = true;
  ui.change(ui.$('#pipeline-mode-upload'));
  assert.equal(ui.$('#pipeline-uploads').hidden, false);

  ui.submit(ui.$('#pipeline-form'));
  await waitFor(() => !ui.$('#banner').hidden);
  assert.ok(ui.text('#banner').includes('Choose both a sample CSV and a lookup CSV'));
  assert.equal(backend.calls.filter((c) => c.method === 'POST').length, 0, 'nothing is sent without both files');

  ui.pickFile(ui.$('#sample-file'), 'sample_orders.csv');
  ui.pickFile(ui.$('#lookup-file'), 'valid_regions.csv');
  ui.submit(ui.$('#pipeline-form'));
  await waitFor(() => backend.calls.some((c) => c.method === 'POST'));
  const body = backend.calls.find((c) => c.method === 'POST').body;
  assert.equal(body.get('sample_file').name, 'sample_orders.csv');
  assert.equal(body.get('lookup_file').name, 'valid_regions.csv');
  await waitFor(() => ui.text('#run-status').includes('completed'));
  assert.ok(ui.$('#banner').hidden, 'the banner clears once an action succeeds');
});

test('executing sends the minimum score, polls to completion and lists the execution', async (t) => {
  const backend = new MockBackend();
  const ui = await open(t, backend);
  await reportReady(ui);

  ui.submit(ui.$('#execute-form'));
  await waitFor(() => ui.text('#exec-header').includes('Execution #100'), { message: 'the new execution' });
  await waitFor(() => ui.$('#report-body .result'), { message: 'results after polling' });

  const post = backend.calls.find((c) => c.method === 'POST' && c.path.endsWith('/execute'));
  assert.equal(post.path, `/runs/${backend.goodRunId}/execute`);
  assert.equal(post.query.min_score, '4');
  assert.equal(post.body, undefined);
  assert.equal(ui.$$('#exec-select option').length, 2);
  assert.equal(ui.$('#exec-select').value, '100');
  assert.ok(ui.window.location.hash.includes('exec=100'));

  // lowering the minimum score re-highlights rules immediately, without calling the backend
  const callsBefore = backend.calls.length;
  ui.change(ui.$('#min-score'), '3');
  assert.ok(ui.text('#rules-summary').includes('8 of 8 rules'));
  assert.equal(ui.$$('#rules-table tr.executes').length, 8);
  assert.equal(ui.text('#execute-hint'), '8 rules rated 3 or higher will run.');
  assert.equal(backend.calls.length, callsBefore);

  ui.submit(ui.$('#execute-form'));
  await waitFor(() => backend.calls.filter((c) => c.path.endsWith('/execute')).length === 2);
  assert.equal(backend.calls.filter((c) => c.path.endsWith('/execute')).at(-1).query.min_score, '3');
});

test('execution upload mode sends target_file', async (t) => {
  const backend = new MockBackend();
  const ui = await open(t, backend);
  await reportReady(ui);
  ui.$('#exec-mode-upload').checked = true;
  ui.change(ui.$('#exec-mode-upload'));
  ui.submit(ui.$('#execute-form'));
  await waitFor(() => !ui.$('#banner').hidden);
  assert.ok(ui.text('#banner').includes('Choose a production CSV'));
  ui.pickFile(ui.$('#target-file'), 'my_prod.csv');
  ui.submit(ui.$('#execute-form'));
  await waitFor(() => backend.calls.some((c) => c.path.endsWith('/execute')));
  assert.equal(backend.calls.find((c) => c.path.endsWith('/execute')).body.get('target_file').name, 'my_prod.csv');
});

// ----------------------------------------------------------------------------------------------
test('LLM- and CSV-derived text is never interpreted as HTML', async (t) => {
  const backend = new MockBackend();
  const evil = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>';
  const good = backend.runs.get(backend.goodRunId);
  Object.assign(good.rules.rules[0], { description: evil, justification: evil, name: '<mark>marked-name</mark>' });
  good.rules.warnings = [evil];
  good.model.model.tables[0].columns[0].description = evil;
  good.model.model.tables[0].name = '<s>tbl</s>';
  const exec = backend.execs.get(backend.fx.execution.exec_id).execution;
  Object.assign(exec.results[0], { rule_name: '<em>rule</em>', description: evil });
  exec.results[0].failed_row_ids[0] = evil;
  exec.results[0].sample_reasons[0].reason = evil;
  exec.results[0].sample_reasons[0].row_id = evil;

  const ui = await open(t, backend);
  await reportReady(ui);

  for (const tag of ['img', 'mark', 's', 'em']) assert.equal(ui.$$(`main ${tag}`).length, 0, `<${tag}> must not be created from data`);
  assert.equal(ui.document.scripts.length, 1, 'only the real app.js script exists');
  assert.equal(ui.window.__xss, undefined);
  assert.ok(ui.text('#rules-table').includes(evil), 'the payload is shown literally');
  assert.ok(ui.text('#rules-table').includes('<mark>marked-name</mark>'));
  assert.ok(ui.text('#model-body').includes('<s>tbl</s>'));
  assert.ok(ui.text('#report-body .chip').includes(evil));
  assert.ok(ui.text('#report-body').includes('<em>rule</em>'));
  assert.ok(ui.text('.warnings').includes(evil));
});

test('API errors are shown readably and the buttons recover', async (t) => {
  const backend = new MockBackend();
  const ui = await open(t, backend);
  await reportReady(ui);

  backend.fail('POST /pipeline/run', 503, 'LLM unavailable: GEMINI_API_KEY is not set');
  ui.submit(ui.$('#pipeline-form'));
  await waitFor(() => ui.text('#banner').includes('GEMINI_API_KEY is not set'));
  assert.equal(ui.$('#banner').hidden, false);
  assert.equal(ui.$('#run-pipeline').disabled, false);
  assert.equal(ui.text('#run-pipeline'), 'Run sample pipeline');
  assert.equal(ui.$$('#run-select option').length, 3, 'no run was added');

  backend.fail(`POST /runs/${backend.goodRunId}/execute`, 422,
    [{ loc: ['query', 'min_score'], msg: 'Input should be greater than or equal to 1', type: 'greater_than_equal' }]);
  ui.submit(ui.$('#execute-form'));
  await waitFor(() => ui.text('#banner') === 'min_score: Input should be greater than or equal to 1');
  assert.equal(ui.$('#run-execute').disabled, false);
});

test('a failed run explains itself and cannot be executed', async (t) => {
  const backend = new MockBackend();
  const ui = await open(t, backend, { hash: `#run=${backend.failedRunId}` });
  await waitFor(() => ui.$('#run-status').classList.contains('failed'));
  assert.ok(ui.text('#run-status').includes('LLMOutputError: model_agent'));
  assert.ok(ui.text('#model-body').includes('not available because the run failed'));
  assert.ok(ui.text('#rules-body').includes('not available because the run failed'));
  assert.equal(ui.$('#run-execute').disabled, true);
  assert.ok(ui.text('#report-body').includes('Run the sample pipeline first'));
});

test('warnings and a failed execution are surfaced instead of silently running nothing', async (t) => {
  const backend = new MockBackend();
  const ui = await open(t, backend);
  await reportReady(ui);

  ui.change(ui.$('#run-select'), backend.lowRunId);
  await waitFor(() => ui.$('.warnings'), { message: 'the warnings box' });
  assert.equal(ui.$$('.warnings li').length, 2);
  assert.ok(ui.text('.warnings').includes('No rule scored 4 or higher'));
  assert.ok(ui.text('#rules-summary').includes('0 of 8 rules'));
  assert.ok(ui.text('#rules-summary').includes('Nothing would run'));
  assert.ok(ui.text('#execute-hint').includes('Lower the minimum'));
  assert.equal(ui.$$('#rules-table tr.executes').length, 0);

  await waitFor(() => ui.$('#report-body .error-box'), { message: 'the execution error' });
  assert.ok(ui.text('#report-body .error-box').includes('No rules rated 4'));
  assert.equal(ui.$$('#report-body .result').length, 0);
  assert.equal(ui.$('#run-execute').disabled, false, 'the user can still lower the minimum score and try again');
});

test('the URL hash restores the selected run and execution, and follows the selection', async (t) => {
  const backend = new MockBackend();
  const original = backend.fx.execution.exec_id;
  const second = { ...clone(backend.fx.execution), exec_id: 50 };
  backend.execs.set(50, { execution: second, pending: [] });
  backend.runs.get(backend.goodRunId).executions.unshift(backend.listItem(second));

  const ui = await open(t, backend, { hash: `#run=${backend.goodRunId}&exec=50` });
  await waitFor(() => ui.text('#exec-header').includes('Execution #50'));
  assert.equal(ui.$('#exec-select').value, '50');

  ui.change(ui.$('#exec-select'), String(original));
  await waitFor(() => ui.text('#exec-header').includes(`Execution #${original}`));
  assert.ok(ui.window.location.hash.includes(`exec=${original}`));
});

test('an unknown run in the URL shows the backend message', async (t) => {
  const ui = await open(t, new MockBackend(), { hash: '#run=doesnotexist' });
  await waitFor(() => ui.text('#banner').includes("unknown run 'doesnotexist'"));
  assert.equal(ui.$('#run-execute').disabled, true);
});

test('switching runs while one is still polling does not leak stale updates', async (t) => {
  const backend = new MockBackend();
  const ui = await open(t, backend, { pollMs: 25 });
  await reportReady(ui);
  ui.submit(ui.$('#pipeline-form'));
  await waitFor(() => ui.text('#run-status').includes('newrun000001'));
  ui.change(ui.$('#run-select'), backend.goodRunId);
  await waitFor(() => ui.text('#run-status').includes(`${backend.goodRunId} completed`));
  await sleep(200);                                                   // the abandoned poll would have fired by now
  assert.ok(ui.text('#run-status').includes(backend.goodRunId));
  assert.ok(ui.window.location.hash.includes(backend.goodRunId));
  assert.equal(ui.$$('#rules-table tbody tr').length, 8);
  assert.deepEqual(ui.errors, []);
});

test('Refresh re-checks the backend and reloads the selected run', async (t) => {
  const backend = new MockBackend();
  const ui = await open(t, backend);
  await reportReady(ui);
  const healthCalls = () => backend.calls.filter((c) => c.path === '/health').length;
  const before = healthCalls();
  ui.click(ui.$('#refresh'));
  await waitFor(() => healthCalls() === before + 1);
  await reportReady(ui);
  assert.equal(ui.$('#run-select').value, backend.goodRunId);
});
