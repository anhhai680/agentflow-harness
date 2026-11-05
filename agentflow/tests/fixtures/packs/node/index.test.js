"use strict";

const test = require("node:test");
const assert = require("node:assert");
const { greet } = require("./index.js");

test("greet says hello to the name it is given", () => {
  assert.strictEqual(greet("Ada"), "Hello, Ada!");
});
