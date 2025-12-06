import io
import json
import oci
import logging
import os
import requests
import time
from typing import List, Dict
import fdk.response

# --- Configurações ---
logger = logging.getLogger("oci-backup-orphan-finder")
logging.basicConfig(level=logging.INFO)

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
REQUEST_TIMEOUT = 10
# Se você tiver mais regiões, adicione aqui (ex: Ashburn: us-ashburn-1)
REGIOES = [{"nome": "Vinhedo", "codigo": "sa-vinhedo-1"}] 

SLACK_TITLE = "🔎 OCI Backup Orphan Finder"
SLACK_COLOR_ORPHAN = "#ff9900" # Laranja para Atenção
SLACK_COLOR_SUCCESS = "#36a64f" # Verde
SLACK_COLOR_ERROR = "#ff0000" # Vermelho

# --- Funções Auxiliares: Slack e Compartimentos ---

def enviar_mensagem_slack(titulo: str, info_operacao: str, detalhes: List[str], cor: str, tempo_execucao: float) -> None:
    """Envia uma mensagem formatada para o Slack."""
    if not SLACK_WEBHOOK_URL:
        logger.warning("SLACK_WEBHOOK_URL não configurado.")
        return
    
    # 1. Cabeçalho e Resumo
    blocks = [
        {
            "type": "header", 
            "text": {
                "type": "plain_text", 
                "text": f"🌐 | Function | {titulo}",
                "emoji": True
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":inform: {info_operacao}"
            }
        }
    ]

    # 2. Lista de Itens (Backups)
    if detalhes:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": ":pushpin: *Lista de Backups Órfãos Encontrados:*"
            }
        })
        detalhes_texto = "\n".join(detalhes)
        if len(detalhes_texto) > 2900:
            detalhes_texto = detalhes_texto[:2900] + "\n... (lista truncada)"
        
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": detalhes_texto}
        })
    else:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "_Nenhum backup órfão encontrado nesta execução._"}]
        })

    # 3. Rodapé Técnico
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn", 
            "text": f"⏱️ Tempo de execução: {tempo_execucao:.2f}s | Região: {REGIOES[0]['nome']}"
        }]
    })

    payload = {"attachments": [{"color": cor, "blocks": blocks}]}

    try:
        requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        logger.error(f"Erro ao enviar Slack: {e}")

def listar_compartimentos(identity_client, tenancy_id) -> List:
    """Lista todos os compartimentos ativos, incluindo a tenancy root."""
    try:
        compartments = oci.pagination.list_call_get_all_results(
            identity_client.list_compartments,
            tenancy_id,
            compartment_id_in_subtree=True,
            access_level="ANY",
            lifecycle_state="ACTIVE"
        ).data
        try:
            root = identity_client.get_compartment(tenancy_id).data
            compartments.append(root)
        except:
            pass
        # Filtra compartimentos de serviço se necessário
        compartments = [c for c in compartments if c.name != "ManagedCompartmentForPaaS"]
        return compartments
    except Exception as e:
        logger.error(f"Erro ao listar compartimentos: {e}")
        return []

# --- Função Principal de Busca ---

def buscar_e_reportar_orfaos(signer) -> List:
    start_time = time.time()
    logger.info("Iniciando busca por backups órfãos...")
    
    identity = oci.identity.IdentityClient(config={}, signer=signer)
    logs_orfaos = []
    total_backups = 0

    compartments = listar_compartimentos(identity, signer.tenancy_id)
    if not compartments:
        enviar_mensagem_slack(SLACK_TITLE, "Erro Crítico: Falha ao listar compartimentos.", [], SLACK_COLOR_ERROR, 0)
        return ["Erro Crítico: Falha ao listar compartimentos."]

    for regiao in REGIOES:
        logger.info(f"--- Região: {regiao['nome']} ({regiao['codigo']}) ---")
        
        try:
            # Cliente para Block Storage (Boot Volumes e Backups)
            block_client = oci.core.BlockstorageClient(config={}, signer=signer)
            block_client.base_client.set_region(regiao["codigo"])
        except Exception as e:
            logger.error(f"Erro ao configurar BlockstorageClient na região {regiao['nome']}: {e}")
            continue

        for comp in compartments:
            try:
                # Listar backups de Boot Volume no estado AVAILABLE
                backups = oci.pagination.list_call_get_all_results(
                    block_client.list_boot_volume_backups,
                    compartment_id=comp.id,
                    lifecycle_state="AVAILABLE"
                ).data
                
                logger.info(f"Compartimento: {comp.name} | Backups encontrados: {len(backups)}")
                total_backups += len(backups)
                
                for backup in backups:
                    # 1. Checagem de Origem (Volume de Boot original)
                    # Se o Volume de Boot original foi apagado (e, por consequência, a VM), este campo fica nulo.
                    volume_origem_deletado = not backup.source_boot_volume_id
                    
                    # 2. Checagem de Política (Defined Tags - OCI Backup Policy)
                    # Defined Tags são usadas para políticas de retenção automatizadas (OCI Backup Policy).
                    # Se estiver vazio, ele não está atrelado a uma política ativa/recente.
                    possui_politica = bool(backup.defined_tags)

                    # 3. Definição de Órfão
                    # É Órfão se não tem mais o volume de origem E não é mantido por política.
                    is_orphan = volume_origem_deletado and not possui_politica
                    
                    if is_orphan:
                        # Link para o Backup no Console OCI
                        backup_url = f"https://cloud.oracle.com/storage/boot-volume-backups/{backup.id}?region={regiao['codigo']}"
                        backup_display = f"<{backup_url}|{backup.display_name} ({backup.time_created.strftime('%Y-%m-%d')})>"
                        
                        logger.warning(f"ÓRFÃO ENCONTRADO: {backup.display_name} em {comp.name}")
                        logs_orfaos.append(
                            f"🪦 *{backup_display}* | Criado em: {backup.time_created.strftime('%Y-%m-%d')} | Tamanho: {backup.size_in_gbs}GB | Comp: `{comp.name}`"
                        )
                    
            except oci.exceptions.ServiceError as e:
                if e.code == "NotAuthorizedOrNotFound":
                    logger.warning(f"Aviso: Sem permissão no compartimento {comp.name}. Pulando.")
                else:
                    logger.error(f"Erro OCI em {comp.name}: {e}")
            except Exception as e:
                logger.error(f"Erro inesperado ao processar backups em {comp.name}: {e}")

    end_time = time.time()
    duration = end_time - start_time
    
    # --- Finalização e Envio de Slack ---
    
    if len(logs_orfaos) > 0:
        info_operacao = f"❌ *ATENÇÃO:* Foram encontrados *{len(logs_orfaos)}* backups de VM órfãos. Revise a lista para possível exclusão e otimização de custos."
        slack_color = SLACK_COLOR_ORPHAN
    elif total_backups == 0:
        info_operacao = f"✅ Nenhuma backup de VM encontrado (ou acessível) para análise."
        slack_color = SLACK_COLOR_SUCCESS
    else:
        info_operacao = f"✅ Todos os {total_backups} backups de VM encontrados estão em conformidade (ou possuem política/volume ativo)."
        slack_color = SLACK_COLOR_SUCCESS
        
    enviar_mensagem_slack(SLACK_TITLE, info_operacao, logs_orfaos, slack_color, duration)
    return logs_orfaos

# --- Handler FDK ---
def handler(ctx, data: io.BytesIO = None):
    """O ponto de entrada da OCI Function."""
    logger.info("Function Backup Orphan Finder iniciada.")
    
    try:
        # Autenticação via Resource Principal (IAM Dynamic Group)
        signer = oci.auth.signers.get_resource_principals_signer()
    except Exception as e:
        logger.critical(f"Erro Auth: Falha ao obter Resource Principal Signer: {e}")
        return fdk.response.Response(ctx, status_code=500, response_data="Erro Auth OCI")

    # A função executa a lógica diretamente, não dependendo de payload
    logs = buscar_e_reportar_orfaos(signer)
    
    return fdk.response.Response(
        ctx, status_code=200, 
        response_data=json.dumps({"status": "Concluido", "logs": logs, "count": len(logs)})
    )
    