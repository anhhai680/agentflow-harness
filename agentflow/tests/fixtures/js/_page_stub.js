// A page for core.js to load in: an empty hash, a history and storage that keep nothing, and no elements.
var location = { hash: "", pathname: "/" };
var history = { replaceState() {} };
var sessionStorage = { getItem() { return null; }, setItem() {} };
var localStorage = { getItem() { return null; }, setItem() {} };
var document = { getElementById() { return null; } };
