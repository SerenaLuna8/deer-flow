import type { Page } from "@playwright/test";

export async function browserPersistenceSecretLocations(
  page: Page,
  secretValues: readonly string[],
): Promise<string[]> {
  return page.evaluate(
    async (needles) => {
      const locations = new Set<string>();
      const inspect = (location: string, value: unknown) => {
        let rendered: string;
        try {
          rendered =
            (typeof value === "string" ? value : JSON.stringify(value)) ??
            String(value);
        } catch {
          rendered = String(value);
        }
        if (needles.some((needle) => needle && rendered.includes(needle))) {
          locations.add(location);
        }
      };

      for (let index = 0; index < localStorage.length; index += 1) {
        const key = localStorage.key(index) ?? "";
        inspect("localStorage", [key, localStorage.getItem(key)]);
      }
      for (let index = 0; index < sessionStorage.length; index += 1) {
        const key = sessionStorage.key(index) ?? "";
        inspect("sessionStorage", [key, sessionStorage.getItem(key)]);
      }
      inspect("cookie", document.cookie);

      if ("caches" in globalThis) {
        for (const name of await caches.keys()) {
          const cache = await caches.open(name);
          for (const request of await cache.keys()) {
            inspect("cacheStorage", [
              name,
              request.url,
              await (await cache.match(request))?.text(),
            ]);
          }
        }
      }
      if (typeof indexedDB.databases === "function") {
        for (const info of await indexedDB.databases()) {
          if (!info.name) continue;
          const databaseName = info.name;
          const database = await new Promise<IDBDatabase>((resolve, reject) => {
            const request = indexedDB.open(databaseName, info.version);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () =>
              reject(request.error ?? new Error("Unable to inspect IndexedDB"));
          });
          try {
            for (const storeName of database.objectStoreNames) {
              const values = await new Promise<unknown[]>((resolve, reject) => {
                const request = database
                  .transaction(storeName, "readonly")
                  .objectStore(storeName)
                  .getAll();
                request.onsuccess = () => resolve(request.result);
                request.onerror = () =>
                  reject(
                    request.error ??
                      new Error("Unable to inspect IndexedDB object store"),
                  );
              });
              inspect("indexedDB", [databaseName, storeName, values]);
            }
          } finally {
            database.close();
          }
        }
      }
      return [...locations].sort();
    },
    [...secretValues],
  );
}
