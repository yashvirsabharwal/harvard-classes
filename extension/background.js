// Opens the finder from the toolbar button, and completes the Q report site's
// single sign-on hop (which needs a real page load) when asked.

chrome.action.onClicked.addListener(() => {
  chrome.tabs.create({ url: chrome.runtime.getURL("finder.html") });
});

function waitForLoad(tabId, timeoutMs) {
  return new Promise((resolve) => {
    const done = () => {
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve();
    };
    const listener = (id, info) => {
      if (id === tabId && info.status === "complete") setTimeout(done, 1500);  // allow the redirect chain to settle
    };
    const timer = setTimeout(done, timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
  });
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.type !== "warmup") return false;
  chrome.tabs.create({ url: msg.url, active: false }, async (tab) => {
    await waitForLoad(tab.id, 20000);
    chrome.tabs.remove(tab.id).catch(() => {});
    reply({ ok: true });
  });
  return true;  // reply asynchronously
});
