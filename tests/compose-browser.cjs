// Synthetic browser acceptance. Requires Playwright in the caller's environment;
// it is intentionally not a production or application dependency.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const base = process.env.WATCHKEEPER_ACCEPTANCE_URL || 'http://127.0.0.1:8775';
const ingress = process.env.WATCHKEEPER_ACCEPTANCE_INGRESS_URL || 'http://127.0.0.1:8776';
const phase = process.argv[2] || 'exercise';
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const unauth = await browser.newContext();
    for (const url of [base, ingress]) {
      for (const path of ['/', '/api/state']) {
        assert.equal((await unauth.request.get(url+path, {headers:{'X-Forwarded-User':'demo','X-Forwarded-Proto':'https','Forwarded':'for=127.0.0.1;proto=https'}})).status(),401);
      }
    }
    assert.equal((await unauth.request.get(base+'/readyz')).status(),200);
    const authenticatedIngress = await unauth.request.get(ingress+'/', {headers:{Authorization:'Basic '+Buffer.from('demo:demo-password-not-for-deployment').toString('base64')}});
    assert.equal(authenticatedIngress.status(),200);
    await unauth.close();
    const context = await browser.newContext({httpCredentials:{username:'demo',password:'demo-password-not-for-deployment'}});
    const page = await context.newPage();
    const errors=[]; page.on('pageerror',e=>errors.push(e.message));
    let confirmDelete=false;
    page.on('dialog',async d=>{if(d.type()==='confirm'){assert.match(d.message(),/external server cannot be recalled/); confirmDelete ? await d.accept() : await d.dismiss();} else {assert.doesNotMatch(d.message(),/undefined|NaN/); await d.accept();}});
    const state=async()=>{const r=await context.request.get(base+'/api/state');assert.equal(r.status(),200);return r.json();};
    const ingest=async()=>context.request.post(base+'/api/observations',{headers:{Authorization:'Bearer demo-intake-token-not-for-deployment'},data:{source_id:'codex-container-review',cursor:'1',available:true,observations:[{thread_id:'synthetic-garden',message_id:'question',revision:'1',source_timestamp:new Date(Date.now()-600000).toISOString(),locator:'Synthetic garden conversation',text:'Please choose three planting zones so the garden guide can be finished.'}]}});
    if(phase==='exercise'){
      assert.equal((await ingest()).status(),200);
      const response=await context.request.get(base+'/api/review-input',{headers:{Authorization:'Bearer demo-reviewer-token-not-for-deployment'}});
      assert.equal(response.status(),200);
      const rows=(await response.json()).observations.filter(o=>o.source_id==='codex-container-review');
      assert.equal(rows.length,1);
      const proposal={project_id:'browser-garden',outcome:'Publish the synthetic garden guide',next_action:'Choose three planting zones',reasoning:'The synthetic conversation explicitly requests a user choice.',evidence_refs:rows.map(o=>o.observation_id),evidence_revisions:Object.fromEntries(rows.map(o=>[o.observation_id,o.revision])),status:'explicit_request',grouping_confidence:'strong'};
      const review=()=>context.request.post(base+'/api/reviews',{headers:{Authorization:'Bearer demo-reviewer-token-not-for-deployment'},data:proposal});
      assert.equal((await review()).status(),200);
      await page.goto(base);
      assert.equal(await page.getByRole('heading',{name:proposal.outcome,exact:true}).count(),1);
      const deliver=async()=>{const response=page.waitForResponse(r=>r.url()===base+'/api/deliver');const navigation=page.waitForNavigation();await page.getByRole('button',{name:'Send pending digest',exact:true}).click();const result=await (await response).json();await navigation;return result;};
      assert.equal((await deliver()).emails,1);
      assert.equal((await deliver()).emails,0);
      for(const action of ['Complete','Reopen','Abandon','Reopen','Archive']) await Promise.all([page.waitForNavigation(),page.getByRole('button',{name:action,exact:true}).click()]);
      assert.equal((await state()).active_projects.length,0);
      await page.getByRole('link',{name:'Archived projects',exact:true}).click();
      assert.equal(await page.getByRole('heading',{name:proposal.outcome,exact:true}).count(),1);
      await Promise.all([page.waitForNavigation(),page.getByRole('button',{name:'Restore',exact:true}).click()]);
      await page.getByRole('link',{name:'Active projects',exact:true}).click();
      const until=new Date(Date.now()+86400000).toISOString().slice(0,16);
      await page.locator('input[name="until"]').fill(until);
      await Promise.all([page.waitForNavigation(),page.getByRole('button',{name:'Snooze',exact:true}).click()]);
      assert.equal((await review()).status(),200);
      assert.equal((await deliver()).emails,0);
      const saved=await state();
      assert.ok(saved.projects[0].snooze_until);
      assert.equal(saved.monitoring.deliveries[0].status,'accepted');
    } else {
      let saved=await state(); assert.equal(saved.projects.length,1);assert.ok(saved.projects[0].snooze_until);
      assert.equal(saved.monitoring.deliveries[0].status,'accepted');
      await page.goto(base);
      assert.match(await page.locator('body').innerText(),/Snoozed until/);
      await page.getByRole('button',{name:'Delete project…',exact:true}).click();
      await page.waitForTimeout(100);
      assert.equal((await state()).projects.length,1);
      confirmDelete=true;
      await Promise.all([page.waitForNavigation(),page.getByRole('button',{name:'Delete project…',exact:true}).click()]);
      saved=await state();assert.equal(saved.projects.length,0);assert.equal(saved.excluded_threads.length,1);
      const replay=await ingest();assert.equal(replay.status(),200);assert.equal((await replay.json()).stored,0);
      assert.equal((await state()).projects.length,0);
    }
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({status:'passed',phase,checks:'direct and ingress authentication, forged proxy headers, digest deduplication, lifecycle, persistent snooze, restart state, confirmed deletion and exclusion'}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
