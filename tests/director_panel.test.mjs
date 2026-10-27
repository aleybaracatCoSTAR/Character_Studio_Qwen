import test from 'node:test';
import assert from 'node:assert/strict';
import {DirectorPanel} from '../frontend/live-director-panel.mjs';

function setup(){
  let click;
  const attributes=new Map(),button={textContent:'',setAttribute:(key,value)=>attributes.set(key,value),
    addEventListener:(name,handler)=>{assert.equal(name,'click');click=handler;}};
  const content={id:'director-content',hidden:false},summary={textContent:'',title:''},empty={hidden:false};
  const panel=new DirectorPanel({button,content,summary,empty});
  return {panel,button,content,summary,empty,attributes,click:()=>click()};
}
const story={enabled:true,index:1,count:5,current:{title:'A small choice'},paused:false,finished:false};

test('director panel starts collapsed with an accessible explicit show button',()=>{
  const {button,content,summary,empty,attributes}=setup();
  assert.equal(content.hidden,true);assert.equal(button.textContent,'Show director controls');
  assert.equal(attributes.get('aria-expanded'),'false');assert.equal(attributes.get('aria-controls'),'director-content');
  assert.equal(summary.textContent,'Director');assert.equal(empty.hidden,false);
});

test('toggle updates hidden content, button wording and expanded state together',()=>{
  const {button,content,attributes,click}=setup();
  click();assert.equal(content.hidden,false);assert.equal(button.textContent,'Hide director controls');
  assert.equal(attributes.get('aria-expanded'),'true');
  click();assert.equal(content.hidden,true);assert.equal(button.textContent,'Show director controls');
  assert.equal(attributes.get('aria-expanded'),'false');
});

test('story updates change only the compact progress summary without reopening details',()=>{
  const {panel,summary,content,attributes}=setup();panel.updateStory(story);
  assert.equal(summary.textContent,'2/5 · A small choice');assert.equal(summary.title,summary.textContent);
  panel.updateStory({...story,index:2,current:{title:'Trust the visitor'},paused:true});
  assert.equal(summary.textContent,'3/5 · Trust the visitor · Held');
  assert.equal(content.hidden,true);assert.equal(attributes.get('aria-expanded'),'false');
});

test('director notes cannot reopen a panel the user collapsed',()=>{
  const {panel,content,empty,click}=setup();click();click();
  panel.noteAvailable();assert.equal(content.hidden,true);assert.equal(empty.hidden,true);
  panel.updateStory(story);assert.equal(content.hidden,true);
  panel.resetNote();assert.equal(content.hidden,true);
});

test('an expanded panel stays expanded through note and story updates or a new call',()=>{
  const {panel,content,click,attributes}=setup();click();
  panel.updateStory(story);panel.noteAvailable();panel.resetNote();
  panel.updateStory({...story,index:4,finished:true});
  assert.equal(content.hidden,false);assert.equal(attributes.get('aria-expanded'),'true');
});

test('disabled story and final completion have concise summaries and honest empty state',()=>{
  const {panel,summary,empty}=setup();panel.updateStory({...story,index:4,finished:true});
  assert.equal(summary.textContent,'Story complete · 5/5');assert.equal(empty.hidden,true);
  panel.updateStory({enabled:false});assert.equal(summary.textContent,'Director');assert.equal(empty.hidden,false);
  panel.noteAvailable();assert.equal(empty.hidden,true);
});

test('beat titles remain literal text rather than HTML',()=>{
  const {panel,summary}=setup();panel.updateStory({...story,current:{title:'<script>doBadThings()</script>'}});
  assert.equal(summary.textContent,'2/5 · <script>doBadThings()</script>');
});
