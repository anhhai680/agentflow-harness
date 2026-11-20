// A page with no DOM: `$` hands back an element that takes any listener, so a view file's top-level wiring loads.
const $ = () => ({ addEventListener() {}, classList: { toggle() {} } });
