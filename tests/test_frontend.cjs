// Run with: node --test tests/test_frontend.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function element() {
    return {value: '', style: {}, children: [], appendChild(child) {this.children.push(child);},
        set innerHTML(value) {this.children = [];}, addEventListener() {}};
}

test('export selection ignores superseded payload responses and blocks premature download', async () => {
    const elements = Object.fromEntries(['exportLogSelect', 'exportPayloads', 'exportKmlButton', 'exportKmlStatus'].map(id => [id, element()]));
    const pending = [];
    const context = vm.createContext({console, Promise, document: {
        readyState: 'loading', addEventListener() {}, getElementById(id) {return elements[id];},
        createElement: element, createTextNode(text) {return {text};}
    }, fetch(url) {return new Promise(resolve => pending.push({url, resolve}));}});
    vm.runInContext(fs.readFileSync('static/js/flight_export.js', 'utf8'), context);
    elements.exportLogSelect.value = 'old.log';
    const old = context.FlightExport.loadPayloads();
    elements.exportLogSelect.value = 'new.log';
    const latest = context.FlightExport.loadPayloads();
    context.FlightExport.download();
    assert.equal(pending.length, 2);
    pending[1].resolve({ok: true, json: async () => ({payloads: [{callsign: 'LATEST', points: 1}]})});
    await latest;
    pending[0].resolve({ok: true, json: async () => ({payloads: [{callsign: 'STALE', points: 1}]})});
    await old;
    assert.equal(elements.exportPayloads.children[0].children[0].value, 'LATEST');
});

test('removing SPOT clears all map layers, state and follow target', () => {
    const removed = [];
    const track = {};
    const names = ['marker', 'path', 'pred_marker', 'pred_path', 'burst_marker', 'abort_marker', 'abort_path'];
    for (const name of names) track[name] = {remove() {removed.push(name);}};
    const context = vm.createContext({console, $() {return {text() {}};}});
    vm.runInContext(fs.readFileSync('static/js/balloon.js', 'utf8'), context);
    context.balloon_positions = {'SPOT': track, 'BALLOON': {}};
    context.balloon_currently_following = 'SPOT';
    context.removeBalloon('SPOT');
    assert.deepEqual(removed, names);
    assert.equal(context.balloon_positions.SPOT, undefined);
    assert.equal(context.balloon_currently_following, 'BALLOON');
});
