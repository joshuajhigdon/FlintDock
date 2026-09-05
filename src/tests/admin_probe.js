/* Disposable real-engine QA only. Never packaged or installed into the actual server. */
import { system, world, Player, EntityAttributeComponent } from "@minecraft/server";
import { ModalFormData } from "@minecraft/server-ui";
import { refreshInfo } from "./main.js"; // QA-only export appended in the disposable copy.
import { REFERENCE, ADMIN_COMMANDS } from "./reference.js";
world.afterEvents.worldLoad.subscribe(() => {
    system.runTimeout(async () => {
        try {
            if (!Object.getOwnPropertyDescriptor(Player.prototype, "playerPermissionLevel")) {
                throw new Error("Player permission API missing");
            }
            if (typeof EntityAttributeComponent.prototype.resetToMaxValue !== "function" ||
                typeof EntityAttributeComponent.prototype.resetToMinValue !== "function") {
                throw new Error("Attribute refill API missing");
            }
            const dim = world.getDimension("overworld");
            if (REFERENCE.schema !== 1 || REFERENCE.entries.length < 47 || ADMIN_COMMANDS.length !== 16 ||
                !REFERENCE.entries.some(entry => entry.id === "core:stop" && entry.where === "Server console only")) {
                throw new Error("Unified help reference is incomplete");
            }
            new ModalFormData().title("QA form construction").textField("Label", "Placeholder", { defaultValue: "saved draft" });
            for (const name of ["help", "menu", "status", "heal", "feed", "inspect", "goto", "bring",
                                "back", "return", "day", "night", "clearweather", "announce", "restart", "cancelrestart"]) {
                // /help returns successCount=0 even for registered commands on BDS.
                // Registration itself is verified by the addon's startup marker.
                dim.runCommand(`help admin:${name}`);
            }
            world.setTimeOfDay(2345);
            try { dim.runCommand("admin:day"); } catch (_) { /* restricted source must not mutate */ }
            await system.waitTicks(2);
            if (Math.abs(world.getTimeOfDay() - 2345) > 40) throw new Error("Server-origin command was not blocked");
            if (await refreshInfo()) throw new Error("Missing manager was incorrectly reported as responsive");
            const request = refreshInfo();
            if (refreshInfo() !== request) throw new Error("Parallel manager checks were not coalesced");
            // Simulate only the manager's reply in this empty QA world, not a restart.
            system.runTimeout(() => dim.runCommand("scriptevent mgrback:info 06:00|06:00|1h"), 1);
            if (!await request) throw new Error("Server-origin manager reply was not accepted");
            console.warn("[ADMIN-QA] PASS: unified command reference, permission API, attribute API, server-origin denial, form defaults, bounded manager timeout, coalesced fresh reply");
        } catch (err) { console.error(`[ADMIN-QA] ${err}`); }
    }, 40);
});
