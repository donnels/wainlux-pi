const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM, VirtualConsole } = require("jsdom");

const html = fs.readFileSync(path.join(__dirname, "../src/ui.html"), "utf8");
const privateDisplay = {masked_fields:["ssid","password","qr-value","card-text","text","comment"],card_content_visible:false,markings_visible:true};
const publicDisplay = {masked_fields:[],card_content_visible:true,markings_visible:false};
const field = (id, type = "text", value = "", extra = {}) => ({id,label:id,type,default:value,editable:true,revealable:true,masked:["text","multiline"].includes(type),options:[],...extra});
const templates = [
  {id:"wifi-login",name:"WiFi login",fields:[field("ssid"),field("password"),field("title","multiline")],elements:[],display:privateDisplay},
  {id:"public-qr",name:"Public QR",fields:[field("public-value","text","",{masked:false})],elements:[],display:publicDisplay},
  {id:"application-card",name:"Application card",fields:[field("locked","multiline","",{editable:false,revealable:false}),field("position","number",10,{masked:false})],elements:[],display:privateDisplay},
  {id:"github-recovery",name:"GitHub recovery",fields:[field("recovery-payload","multiline","",{editable:true,revealable:true,masked:true}),field("login-url","text","https://github.com/login",{masked:true}),field("front-context-y","number",1.75,{masked:false}),field("back-context-y","number",1.75,{masked:false})],elements:[{id:"recovery-qr",side:"front",bindings:{"content.text":"recovery-payload"}},{id:"login-qr",side:"back",bindings:{"content.text":"login-url"}}],display:privateDisplay},
];
const machine = {model:"Simulator",reach_mm:[80,75],settings:[]};
const mode = {mode:"simulator",real:false,real_available:false};
const layout = {reach_mm:[80,75],piece:{x:1,y:10.64,w:85.45,h:53.72},etch:{x:7.45,y:10.64,w:72.55,h:53.72}};
const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path d="M0 0h10v1H1v8h9v1H0z" fill="black"/></svg>';
const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXi8AAAAASUVORK5CYII=";
const stage = name => ({name,goal:name,burns:[{x_mm:1,y_mm:10,w_mm:30,h_mm:30,width_px:10,height_px:10,svg,png}]});

async function open(backend) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", error => errors.push(error));
  const dom = new JSDOM(html, {
    url:"http://localhost/",runScripts:"dangerously",virtualConsole,
    beforeParse(window) {
      const media = {matches:Boolean(backend.systemDark),addEventListener:(_,listener) => { backend.themeChange = listener; }};
      window.matchMedia = () => media;
      backend.media = media;
      if (backend.savedTheme) window.localStorage.setItem("laser_theme", backend.savedTheme);
      window.setInterval = () => 0;
      window.structuredClone = structuredClone;
      window.alert = value => { throw new Error(value); };
      window.confirm = () => true;
      window.fetch = async (url, options) => {
        (backend.requests ||= []).push(url);
        const body = options?.body ? JSON.parse(options.body) : null;
        let value;
        switch (url) {
          case "/api/steps": value = {machine,defaults:{text:"",size_mm:20,x_mm:0,y_mm:0},steps:[{step:"frame",title:"Frame"},{step:"qr",title:"QR"},{step:"talk",title:"Talk"}]}; break;
          case "/api/tests/preview":
            backend.testPreviews ||= [];
            backend.testPreviews.push(body);
            value = body.step === "talk"
              ? {goal:"Simulator answers; nothing moves.",svg:null,note:"This step has no raster pattern."}
              : {goal:`${body.step} expected pattern at ${body.params.x_mm},${body.params.y_mm} mm`,svg,note:"Synthetic pattern preview only; not a burn result."};
            break;
          case "/api/materials": value = [
            {material:{id:"bamboo-ntag215-light",name:"Card",role:"piece",size_mm:[85.45,53.72]},values:{}},
            {material:{id:"backing-first",name:"Backing",role:"backing",size_mm:[80,75]},values:{}},
          ]; break;
          case "/api/templates": value = templates; break;
          case "/api/log": value = []; break;
          case "/api/state":
            if (options?.method === "DELETE") {
              backend.clearCalls = (backend.clearCalls || 0) + 1;
              backend.review = null;
              backend.state = {...backend.state,attempt:null,running:false,outcome:null,progress:null,review_id:null,accepted_stages:null};
              value = {cleared:true,run_log_retained:true};
            } else value = backend.state;
            break;
          case "/api/jobs/review":
            if (options?.method === "DELETE") { backend.review = null; backend.state.review_id = null; value = {discarded:true}; }
            else value = backend.review;
            break;
          case "/api/jobs/form": {
            const template = templates.find(template => template.id === body.template);
            const values = backend.review?.fields || Object.fromEntries(template.fields.map(field => [field.id,{value:field.default,provided:false}]));
            for (const [id,value] of Object.entries(body.fields)) values[id] = {value,provided:true};
            backend.lastChanges = body.fields;
            const stages = template.id === "github-recovery" ? [{...stage("front"),side:"front"},{...stage("back"),side:"back"}] : [stage("guide"),stage("content")];
            backend.review = {review_id:"review-" + (backend.revision = (backend.revision || 0) + 1),template,fields:values,job:{template:body.template,piece:body.piece,backing:body.backing,placement:body.placement,guide:body.guide,items:[],values:{}},layout,display:template.display,stages};
            backend.state.review_id = backend.review.review_id;
            value = backend.review; break;
          }
          case "/api/attempt/preview": value = {attempt_id:backend.state.attempt.id,layout,display:privateDisplay,stages:[stage(backend.activeStage)]}; break;
          case "/api/mode": backend.state.mode = {...backend.state.mode,real:body.real,mode:body.real?"real":"simulator"}; value = backend.state.mode; break;
          default: throw new Error("Unexpected request: " + url);
        }
        return {ok:true,json:async () => structuredClone(value)};
      };
    },
  });
  for (let turn = 0; turn < 100 && dom.window.document.getElementById("status").textContent === "Connecting..."; turn++) {
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.deepEqual(errors, []);
  assert(!dom.window.document.getElementById("status").textContent.startsWith("Connecting"), dom.window.document.getElementById("status").textContent + " requests=" + (backend.requests||[]).join(",") + " " + errors.map(error=>error.message).join("; "));
  return dom;
}

async function main() {
  const backend = {state:{attempt:null,running:false,outcome:null,progress:null,mode}};
  const dom = await open(backend);
  const window = dom.window, document = window.document;
  const element = id => document.getElementById(id);
  const toggle = id => document.querySelector(`button[aria-controls="${id}"]`);
  assert.equal(document.documentElement.dataset.themePreference, "system");
  assert.equal(document.documentElement.dataset.theme, "light");
  assert.equal(document.documentElement.dataset.previewPolarity, "dark");
  assert.equal(element("step").value, "frame");
  assert(element("test-preview-goal").textContent.includes("frame expected pattern"));
  assert.equal(element("test-pattern-image").hidden, false);
  element("step").value = "talk";
  element("step").dispatchEvent(new window.Event("change", {bubbles:true}));
  await new Promise(resolve => setTimeout(resolve, 220));
  assert.equal(element("test-pattern-image").hidden, true);
  assert.equal(element("test-pattern-empty").hidden, false);
  assert(element("test-preview-goal").textContent.includes("Simulator answers"));
  element("step").value = "qr";
  element("step").dispatchEvent(new window.Event("change", {bubbles:true}));
  element("text").value = "SYNTHETIC PREVIEW ONLY";
  element("text").dispatchEvent(new window.Event("input", {bubbles:true}));
  await new Promise(resolve => setTimeout(resolve, 220));
  assert.equal(backend.testPreviews.at(-1).params.text,"SYNTHETIC PREVIEW ONLY");
  assert.equal(element("test-pattern-image").hidden,false);
  assert(element("test-preview-note").textContent.includes("not a burn result"));
    const bandWait = window.progressText({phase:"waiting",percent:97,band:2,bands:5,row_start:500,row_end:620,previous_assumed:true});
    assert(bandWait.includes("Band 2 of 5") && bandWait.includes("raster rows 501-620"));
    assert(bandWait.includes("97% of this band only") && bandWait.includes("not the whole card") && bandWait.includes("90 seconds"));
    const bandStart = window.progressText({phase:"starting",percent:null,band:3,bands:5,row_start:700,row_end:810,previous_assumed:true});
    assert(bandStart.toLowerCase().includes("previous band was assumed complete"));
    assert(window.bandTimingText([{band:2,bands:5,row_start:500,row_end:620,transfer_ms:12000,setup_ms:4900,wait_ms:90000,phase:"assumed_complete"}]).includes("transfer 12.0s, setup 4.9s, laser wait 90.0s; completion assumed"));
  assert.equal(document.querySelector("footer").textContent.trim(), "Copyright (c) 2026 Sean Donnellan");
  assert.equal(document.querySelector('meta[name="author"]').content, "Sean Donnellan");
  element("theme").value = "dark";
  element("theme").dispatchEvent(new window.Event("change"));
  assert.equal(document.documentElement.dataset.theme, "dark");
  assert.equal(window.localStorage.getItem("laser_theme"), "dark");
  backend.media.matches = false; backend.themeChange();
  assert.equal(document.documentElement.dataset.theme, "dark", "explicit dark ignores system changes");
  window.setTheme("system"); backend.media.matches = true; backend.themeChange();
  assert.equal(document.documentElement.dataset.theme, "dark");
  backend.media.matches = false; backend.themeChange();
  assert.equal(document.documentElement.dataset.theme, "light");
  assert.equal(element("template-fields").children.length, 0, "load type before showing fields");
  assert.equal(element("preview-card").disabled, true);
  window.renderTemplate(templates[0]); window.controls();
  assert.equal(element("field-public-value"), null, "undefined elements never appear");
  for (const [id,value] of [["ssid","Synthetic network"],["password","Synthetic password"],["title","TOP\nBOTTOM"]]) {
    element("field-" + id).value = value;
    element("field-" + id).dispatchEvent(new window.Event("input", {bubbles:true}));
  }
  for (const id of ["field-ssid","field-password","text"]) assert.equal(element(id).type, "password");
  assert.equal(element("field-title").hidden, true);
  toggle("field-ssid").click(); assert.equal(element("field-ssid").type, "text");
  toggle("field-ssid").click(); assert.equal(element("field-ssid").type, "password");
  toggle("field-title").click(); assert.equal(element("field-title").hidden, false);
  toggle("field-title").click(); assert.equal(element("field-title").value, "TOP\nBOTTOM");
  assert.equal(element("field-title").hidden, true);
  await window.prepareCard();
  assert.equal(element("preview-card").parentElement, element("burn-guide").parentElement);
  assert.equal(element("laser-output").hidden, false);
  const outputContent = document.querySelector('.output-image[data-preview-layer="content"]');
  const outputGuide = document.querySelector('.output-image[data-preview-layer="guide"]');
  assert.equal(outputContent.getAttribute("src"), png);
  assert.equal(outputContent.style.display, "none");
  assert.equal(outputGuide.style.display, "");
  const content = document.querySelector('[data-preview-layer="content"]');
  const guide = document.querySelector('[data-preview-layer="guide"]');
  assert.equal(content.style.display, "none"); assert.equal(guide.style.display, "");
  element("show-card").click(); assert.equal(content.style.display, "");
  assert.equal(outputContent.style.display, "");
  window.setTheme("dark");
  assert.equal(content.style.display, "", "theme changes do not change privacy visibility");
  assert.equal(document.documentElement.dataset.previewPolarity, "dark", "dark theme does not invert burn marks");
  const originalPng = outputContent.getAttribute("src");
  const originalSvg = content.getAttribute("href");
  element("preview-polarity").value = "light";
  element("preview-polarity").dispatchEvent(new window.Event("change"));
  assert.equal(document.documentElement.dataset.previewPolarity, "light");
  assert.equal(outputContent.getAttribute("src"), originalPng, "polarity does not change raster pixels");
  assert.equal(content.getAttribute("href"), originalSvg);
  assert.equal(outputContent.style.display, "");
  assert.equal(outputGuide.style.display, "");
  window.setTheme("light");
  assert.equal(document.documentElement.dataset.previewPolarity, "light");
  window.setPreviewPolarity("dark");
  assert.equal(document.documentElement.dataset.previewPolarity, "dark");
  const rasterMarkup = decodeURIComponent(content.getAttribute("href").split(",").slice(1).join(","));
  assert(!rasterMarkup.includes('fill="white"'), "preview has no bright raster background");
  element("show-card").click(); assert.equal(content.style.display, "none");
  assert.equal(outputContent.style.display, "none");
  element("show-markings").click(); assert.equal(guide.style.display, "none");
  element("show-markings").click(); assert.equal(guide.style.display, "");
  element("template").value = "public-qr";
  element("template").dispatchEvent(new window.Event("change", {bubbles:true}));
  await new Promise(setImmediate);
  assert.equal(element("field-ssid"), null); assert.equal(element("field-password"), null);
  element("field-public-value").value = "Public sample";
  element("field-public-value").dispatchEvent(new window.Event("input", {bubbles:true}));
  await window.prepareCard();
  assert.equal(document.querySelector('[data-preview-layer="content"]').style.display, "");
  assert.equal(document.querySelector('[data-preview-layer="guide"]').style.display, "none");
  const received = await open(backend);
  assert.equal(received.window.document.getElementById("field-public-value").value, "Public sample");
  assert.equal(received.window.document.getElementById("laser-output").hidden, false);
  received.window.close();
  element("field-public-value").value = "Unsaved change";
  element("field-public-value").dispatchEvent(new window.Event("input", {bubbles:true}));
  await window.poll();
  assert.equal(element("field-public-value").value, "Unsaved change", "polling must not restore the older review over edits");
  dom.window.close();

  backend.state = {attempt:{id:"retained-guide",job:{items:[]},goal:"Guide"},running:false,outcome:{ok:true,saw:"assumed complete"},progress:99,mode};
  backend.activeStage = "guide";
  const guidePage = await open(backend);
  assert.equal(guidePage.window.document.querySelector('[data-preview-layer="guide"]').style.display, "");
  assert.equal(guidePage.window.document.getElementById("show-card").disabled, true);
  guidePage.window.close();
  backend.state.attempt.id = "retained-content"; backend.activeStage = "content";
  const contentPage = await open(backend);
  assert.equal(contentPage.window.document.querySelector('[data-preview-layer="content"]').style.display, "none");
  contentPage.window.close();
  backend.state = {attempt:null,running:false,outcome:null,progress:null,mode,review_id:"locked-review"};
  backend.review = {review_id:"locked-review",template:templates[2],fields:{locked:{provided:true,value:null},position:{provided:true,value:10}},job:{template:"application-card",piece:"bamboo-ntag215-light",backing:"backing-first",placement:"left",guide:true,items:[],values:{}},layout,display:privateDisplay,stages:[stage("guide"),stage("content")]};
  const restricted = await open(backend);
  const restrictedDoc = restricted.window.document;
  assert.equal(restrictedDoc.getElementById("field-ssid"), null);
  assert.equal(restrictedDoc.getElementById("field-locked").value, "");
  assert.equal(restrictedDoc.getElementById("field-locked").disabled, true);
  assert.equal(restrictedDoc.querySelector('button[aria-controls="field-locked"]').hidden, true);
  restrictedDoc.getElementById("show-card").click();
  assert.equal(restrictedDoc.querySelector('.output-image[data-preview-layer="content"]').style.display, "");
  await restricted.window.prepareCard();
  assert.deepEqual(backend.lastChanges, {}, "restricted source values are not copied back by the browser");
  restricted.window.close();
  backend.savedTheme = "dark";
  const savedThemePage = await open(backend);
  assert.equal(savedThemePage.window.document.documentElement.dataset.theme, "dark");
  assert.equal(savedThemePage.window.document.documentElement.dataset.previewPolarity, "dark", "saved dark theme still defaults to dark burn marks");
  assert.equal(savedThemePage.window.document.getElementById("theme").value, "dark");
  savedThemePage.window.close();
  const liveMode = {mode:"simulator",real:false,real_available:true};
  backend.state = {attempt:{id:"stale-live-attempt",review_id:"mode-review",job:{items:[]},goal:"Old attempt"},running:false,outcome:null,progress:null,mode:liveMode};
  backend.activeStage = "guide";
  backend.review = {review_id:"mode-review",template:templates[2],fields:{locked:{provided:false,value:null},position:{provided:true,value:10}},job:{template:"application-card",piece:"bamboo-ntag215-light",backing:"backing-first",placement:"left",guide:false,items:[],values:{}},layout,display:privateDisplay,stages:[stage("content")]};
  const modePage = await open(backend); const modeDoc = modePage.window.document;
  assert.equal(modeDoc.getElementById("mode").disabled,true,"active review freezes mode");
  backend.state.attempt = null;
  await modePage.window.poll();
  assert.equal(modeDoc.getElementById("mode").disabled,false,"idle API state clears stale browser freeze");
  modeDoc.getElementById("mode").value = "real";
  modeDoc.getElementById("mode").dispatchEvent(new modePage.window.Event("change"));
  for (let turn=0;turn<20 && modeDoc.getElementById("mode").disabled;turn++) await new Promise(resolve=>setTimeout(resolve,5));
  assert.equal(backend.state.mode.real,true,"user mode switch reaches the API after idle clears");
  modePage.window.close();
  const reuseBackend = {state:{attempt:null,running:false,outcome:null,progress:null,mode,review_id:"reuse-review"}};
  reuseBackend.review = {review_id:"reuse-review",template:templates[2],fields:{locked:{provided:false,value:null},position:{provided:true,value:10}},job:{template:"application-card",piece:"bamboo-ntag215-light",backing:"backing-first",placement:"left",guide:false,items:[],values:{}},layout,display:privateDisplay,stages:[stage("content")]};
  const reusePage = await open(reuseBackend);
  const reuseDoc = reusePage.window.document;
  assert.equal(reuseDoc.getElementById("burn-guide").disabled, true);
  assert.equal(reuseDoc.getElementById("placement-check").hidden, false, `reuse gate: ${reuseDoc.getElementById("status").textContent}; stages=${[...reuseDoc.querySelectorAll(".output-stage h3")].map(node=>node.textContent).join(",")}; guide=${backend.review.job.guide}`);
  assert.equal(reuseDoc.getElementById("burn-card").disabled, true);
  reuseDoc.getElementById("card-placed").checked = true;
  reuseDoc.getElementById("card-placed").dispatchEvent(new reusePage.window.Event("change"));
  assert.equal(reuseDoc.getElementById("burn-card").disabled, false, "existing guide can be reused after explicit placement confirmation");
  reusePage.window.close();
  backend.state = {attempt:null,running:false,outcome:null,progress:null,mode,review_id:"two-sides",accepted_stages:[]};
  backend.review = {review_id:"two-sides",accepted_stages:[],template:{...templates[2],elements:[{id:"front-item",side:"front",bindings:{"content.text":"locked"}},{id:"back-item",side:"back",bindings:{"content.text":"position"}}]},fields:{locked:{provided:true,value:null},position:{provided:true,value:10}},job:{template:"application-card",piece:"bamboo-ntag215-light",backing:"backing-first",placement:"left",guide:false,items:[],values:{}},layout,display:privateDisplay,stages:[{...stage("front"),side:"front"},{...stage("back"),side:"back"}]};
  const sidesPage = await open(backend); const sidesDoc = sidesPage.window.document;
  assert.equal(sidesDoc.getElementById("side-control").hidden, false);
  assert.equal(sidesDoc.querySelectorAll(".output-stage").length, 1, "front and back rasters never overlay");
  sidesPage.window.selectCardSide("back");
  sidesDoc.getElementById("card-placed").checked = true; sidesPage.window.controls();
  assert.equal(sidesDoc.getElementById("burn-card").disabled, true, "back locked until front acceptance");
  assert(sidesDoc.getElementById("placement-label").textContent.includes("flipped"));
  backend.state.accepted_stages = [0]; await sidesPage.window.poll();
  assert.equal(sidesDoc.getElementById("burn-card").disabled, false);
  assert.equal(sidesDoc.querySelectorAll(".output-stage").length, 1);
  sidesPage.window.close();
  backend.state = {attempt:null,running:false,outcome:null,progress:null,mode,review_id:"github-review"};
  backend.review = {review_id:"github-review",template:templates.find(template=>template.id==="github-recovery"),accepted_stages:[],fields:{"recovery-payload":{provided:true,value:null},"login-url":{provided:true,value:"https://github.com/login"},"front-context-y":{provided:true,value:1.75},"back-context-y":{provided:true,value:1.75}},job:{template:"github-recovery",piece:"bamboo-ntag215-light",backing:"backing-first",placement:"left",guide:false,items:[],values:{}},layout,display:privateDisplay,stages:[{...stage("front"),side:"front"},{...stage("back"),side:"back"}]};
  const githubPage = await open(backend); const githubDoc = githubPage.window.document;
  githubPage.window.selectCardSide("front");
  const recovery = githubDoc.getElementById("field-recovery-payload");
  assert(recovery && recovery.definition.editable && recovery.definition.revealable);
  assert.equal(recovery.hidden,true,"recovery payload is masked on load");
  const recoveryToggle = githubDoc.querySelector('button[aria-controls="field-recovery-payload"]');
  assert.equal(recoveryToggle.hidden,false);
  recoveryToggle.click(); assert.equal(recovery.hidden,false);
  recovery.value = "SYNTHETIC FIVE-CHAR-DASH DEMO";
  recovery.dispatchEvent(new githubPage.window.Event("input", {bubbles:true}));
  await githubPage.window.prepareCard();
  assert.equal(backend.lastChanges["recovery-payload"],"SYNTHETIC FIVE-CHAR-DASH DEMO");
  assert.equal(githubDoc.getElementById("card-side").value,"front");
  githubPage.window.selectCardSide("back");
  assert.equal(githubDoc.getElementById("field-recovery-payload").closest("label").hidden,true,"front payload editor is hidden on the back side");
  assert.equal(githubDoc.querySelector('button[aria-controls="field-recovery-payload"]').hidden,false,"real codes can be entered and optionally revealed");
  const loginUrl = githubDoc.getElementById("field-login-url");
  loginUrl.value = "https://github.com/new-login";
  loginUrl.dispatchEvent(new githubPage.window.Event("input", {bubbles:true}));
  await githubPage.window.prepareCard();
  assert.equal(backend.lastChanges["login-url"],"https://github.com/new-login");
  assert.equal(githubDoc.getElementById("card-side").value,"back","previewing the edited login URL keeps the back selected");
  assert.equal(githubDoc.querySelector(".output-stage h3").textContent,"Back content");
  githubPage.window.close();
  const clearBackend = {state:{attempt:null,running:false,outcome:null,progress:null,mode,review_id:"clear-review"}};
  clearBackend.review = {review_id:"clear-review",template:templates[0],fields:{ssid:{provided:true,value:"Synthetic network"},password:{provided:true,value:"synthetic password"},title:{provided:false,value:""}},job:{template:"wifi-login",piece:"bamboo-ntag215-light",backing:"backing-first",placement:"left",guide:true,items:[],values:{}},layout,display:privateDisplay,stages:[stage("guide"),stage("content")]};
  const clearPage = await open(clearBackend); const clearDoc = clearPage.window.document;
  assert.equal(clearDoc.getElementById("clear-pi").textContent,"Sign out / Clear Pi");
  assert.equal(clearDoc.getElementById("field-password").value,"synthetic password");
  await clearPage.window.clearPi();
  assert.equal(clearBackend.clearCalls,1);
  assert.equal(clearBackend.review,null);
  assert.equal(clearBackend.state.review_id,null);
  assert.equal(clearDoc.getElementById("field-password"),null,"clear removes private review fields from the browser");
  assert(clearDoc.getElementById("status").textContent.includes("run history retained"));
  clearPage.window.close();
  console.log("UI checks passed: VSR logo, Light/Dark/System, saved preference, private fields, preview layers and refresh recovery.");
}

main().catch(error => { console.error(error); process.exitCode = 1; });