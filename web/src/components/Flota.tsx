import { useOrganigrama, useOrganizacionNativa, useRuntimesNativos, useSalud } from "@/lib/queries";
import { marcaComprobacion } from "@/lib/flota";
import { BannerSalud } from "@/components/BannerSalud";
import { TablaJerarquia } from "@/components/TablaJerarquia";
import { TablaOrganizacionNativa } from "@/components/TablaOrganizacionNativa";
import { TablaRuntimesNativos } from "@/components/TablaRuntimesNativos";

/** La consola operacional de flota. Read-only en beta a propósito [ADR-002
 *  Decisión 5/8] — ni un control de aquí emite un verbo mutador. El estado
 *  NATIVO (G7-G10) va primero porque es el que este console existe para servir;
 *  el organigrama LEGADO va después, con su insignia `authority:false` en el
 *  propio componente — nunca fundidos en una sola vista [Decisión 6: "Native
 *  clients and the v1 console use `GET /native/v1/organization`; they do not
 *  merge the legacy endpoint into an authoritative view"].
 *
 *  Comparte `Puerta`/token/censo con la bandeja — no reimplementa login. */
export function Flota({ entrado }: { entrado: boolean }) {
  const salud = useSalud(entrado);
  const organizacionNativa = useOrganizacionNativa(entrado);
  const runtimesNativos = useRuntimesNativos(entrado);
  const organigrama = useOrganigrama(entrado);

  // D10 [design §2.3, caso C7] — la MARCA del cliente, separada del dato del
  // servidor (que lleva su propio recibo): "última comprobación HH:MM" visible
  // para que una vista que dejó de refrescar NO se lea como dato fresco ni como
  // proceso muerto. Es la más reciente de las dos queries nativas.
  const ultimaComprobacion = Math.max(
    organizacionNativa.dataUpdatedAt ?? 0,
    runtimesNativos.dataUpdatedAt ?? 0,
  );

  return (
    <div className="flex-1 overflow-y-auto">
      <BannerSalud salud={salud.data} estado={undefined} />
      <p className="px-4 pb-1 text-xs text-apagado" role="status">
        {marcaComprobacion(ultimaComprobacion || undefined, Date.now())}
      </p>
      <TablaOrganizacionNativa query={organizacionNativa} />
      <TablaRuntimesNativos organizacion={organizacionNativa} runtimes={runtimesNativos} />
      <hr className="mx-4 border-linea" />
      <TablaJerarquia query={organigrama} />
    </div>
  );
}
