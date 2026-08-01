"""ModelSpec — 策略 YAML `models` 节与 model_meta.json v3 的解析/校验。

模型对引擎而言是抽象的打分公式：引擎不认识模型的意图（选股/离场/
市场状态……），只负责数据管线的唯一性与因果正确。唯一的区分是
**输入数据何时存在**（scope），由特征自动推导，不是语义声明：

  - 无 state_features → scope=panel：特征是行情列，preload 随因子
    物化批量求值，写 ml_<name> 列，与因子列同通道消费
  - 有 state_features → scope=holding：特征依赖账户状态，决策时点
    逐持仓求值，分数注入该持仓的 bar dict，策略自行解释

特征契约（feature_order = factors + raw + state_features）以 meta 为准：
meta 由训练侧 export 写入，加载期 artifact/meta/特征缺失一律 fail-fast。

首次训练时 meta 尚不存在——训练脚本以 require_meta=False 从 YAML
内联 features 引导（bootstrap），导出 meta 后引擎路径才可用。

YAML 形态：
    models:
      alpha_xs:                        # 模型名 → 分数列 ml_alpha_xs
        artifact: ml_model/alpha_xs.onnx
        # meta 缺省 = 同名 .meta.json
        # features: {factors: [...], raw: [...]}   # 仅首次训练引导用
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

META_VERSION = 3
# v3：ret_from_entry 从 hfq 收盘/裸买入价改为裸价口径（价格体系契约），
# 旧 v2 meta 一律拒绝，含该特征的模型必须重新训练

SCOPE_PANEL = "panel"
SCOPE_HOLDING = "holding"

SUPPORTED_STATE_FEATURES = ("hold_days", "ret_from_entry")
POST_TRANSFORMS = ("none", "xs_rank", "xs_zscore")


@dataclass
class ModelSpec:
    """一个 ML 模型的完整运行时契约（意图中性）。

    Attributes:
        name: 模型名（models 节的键），分数列名为 ``ml_<name>``。
        artifact: ONNX 模型文件绝对路径。
        features: ddup 因子名特征（经因子闭包物化）。
        raw_features: backend 原始列特征（并入 REQUIRED_FIELDS 请求）。
        state_features: 账户态特征（引擎决策时点按统一公式计算）。
        post_transform: 分数截面后变换（none/xs_rank/xs_zscore）。
        scaler_mean / scaler_std: StandardScaler 参数（推理时应用）。
        meta: model_meta.json 原文（含 label/train_window/metrics 等标注）。
    """

    name: str
    artifact: str
    features: list[str]
    raw_features: list[str] = field(default_factory=list)
    state_features: list[str] = field(default_factory=list)
    post_transform: str = "none"
    scaler_mean: list[float] = field(default_factory=list)
    scaler_std: list[float] = field(default_factory=list)
    meta: dict = field(default_factory=dict, repr=False)

    @property
    def column(self) -> str:
        """模型的分数列名。"""
        return f"ml_{self.name}"

    @property
    def scope(self) -> str:
        """输入数据时序域，由特征推导：含账户态特征 → holding。"""
        return SCOPE_HOLDING if self.state_features else SCOPE_PANEL

    @property
    def feature_order(self) -> list[str]:
        """模型输入向量的列序（训练/推理同一契约）。"""
        return self.features + self.raw_features + self.state_features

    @classmethod
    def from_dict(
        cls,
        name: str,
        d: dict,
        strategy_dir: str = "",
        *,
        require_meta: bool = True,
    ) -> "ModelSpec":
        """从 YAML models 条目 + meta 构造。

        Args:
            name: models 节的键。
            d: YAML 条目 dict（artifact 必填；role/threshold/meta/features 可选）。
            strategy_dir: 策略 YAML 所在目录，用于解析相对路径。
            require_meta: True（引擎路径）时 meta 缺失直接报错；
                False（训练引导）时允许从 YAML 内联 features 构造。
        """
        if not isinstance(d, dict):
            raise ValueError(f"models.{name} 必须是 mapping: {d!r}")
        artifact_raw = d.get("artifact")
        if not artifact_raw:
            raise ValueError(f"models.{name} 缺少必填键 artifact")
        p = Path(artifact_raw)
        if not p.is_absolute() and strategy_dir:
            p = Path(strategy_dir) / p
        p = p.resolve()
        if not p.exists():
            raise ValueError(f"models.{name} artifact 不存在: {p}")
        if p.suffix != ".onnx":
            raise ValueError(f"models.{name} artifact 必须是 .onnx 文件: {p}")

        meta_path = d.get("meta")
        if meta_path:
            mp = Path(meta_path)
            if not mp.is_absolute() and strategy_dir:
                mp = Path(strategy_dir) / mp
        else:
            mp = p.with_suffix(".meta.json")
        meta: dict = {}
        if mp.exists():
            with open(mp, encoding="utf-8") as f:
                meta = json.load(f)
            version = meta.get("version")
            if version != META_VERSION:
                raise ValueError(
                    f"models.{name} meta 版本 {version!r} 不受支持，"
                    f"需要 version={META_VERSION}（由 scripts/ml_train.py 导出）: {mp}"
                )
        elif require_meta:
            raise ValueError(
                f"models.{name} 缺少 meta 文件: {mp} —— "
                "先运行 scripts/ml_train.py 训练导出"
            )

        # 旧版 meta 的 track 键与 YAML role 键已废弃：scope 由 state_features 推导
        if "role" in d:
            logger.warning(
                "models.%s 的 role 键已废弃（scope 由 state_features 自动推导），忽略",
                name,
            )

        meta_features = meta.get("features") or {}
        yaml_features = d.get("features") or {}
        if meta_features:
            features = list(meta_features.get("factors", []))
            raw_features = list(meta_features.get("raw", []))
            if yaml_features:
                y_factors = list(yaml_features.get("factors", []))
                y_raw = list(yaml_features.get("raw", []))
                if y_factors != features or y_raw != raw_features:
                    raise ValueError(
                        f"models.{name} YAML 内联 features 与 meta 不一致 "
                        f"（meta 为准，请删除 YAML 内联 features 或重新训练）"
                    )
        elif yaml_features:
            features = list(yaml_features.get("factors", []))
            raw_features = list(yaml_features.get("raw", []))
        else:
            raise ValueError(
                f"models.{name} 无特征契约（meta 缺失且 YAML 未内联 features）"
            )
        if not features and not raw_features:
            raise ValueError(f"models.{name} 特征列表为空")

        state_features = list(
            meta.get("state_features", yaml_features.get("state", []))
        )
        bad_state = [s for s in state_features if s not in SUPPORTED_STATE_FEATURES]
        if bad_state:
            raise ValueError(
                f"models.{name} 不支持的 state_features: {bad_state}，"
                f"支持: {list(SUPPORTED_STATE_FEATURES)}"
            )

        post_transform = meta.get("post_transform", "none")
        if post_transform not in POST_TRANSFORMS:
            raise ValueError(
                f"models.{name} 未知 post_transform {post_transform!r}，"
                f"支持: {list(POST_TRANSFORMS)}"
            )

        # scaler 维度必须等于特征契约维度（meta 与特征不一致 = 静默错分，
        # fail-fast；旧版/手改 meta 在此拦截）
        scaler_mean = list(meta.get("scaler_mean", []))
        scaler_std = list(meta.get("scaler_std", []))
        n_feat = len(features) + len(raw_features) + len(state_features)
        if scaler_mean and len(scaler_mean) != n_feat:
            raise ValueError(
                f"models.{name} scaler_mean 维度 {len(scaler_mean)} != 特征维度 "
                f"{n_feat}——meta 与特征契约不一致（请重新训练导出）"
            )
        if scaler_std and len(scaler_std) != n_feat:
            raise ValueError(
                f"models.{name} scaler_std 维度 {len(scaler_std)} != 特征维度 "
                f"{n_feat}——meta 与特征契约不一致（请重新训练导出）"
            )

        return cls(
            name=name,
            artifact=str(p),
            features=features,
            raw_features=raw_features,
            state_features=state_features,
            post_transform=post_transform,
            scaler_mean=scaler_mean,
            scaler_std=scaler_std,
            meta=meta,
        )

    def run_summary(self) -> dict:
        """写入 runs.config_json 的模型摘要（版本可追溯）。"""
        return {
            "name": self.name,
            "scope": self.scope,
            "label": self.meta.get("label"),
            "train_window": self.meta.get("train_window"),
            "artifact_sha256": self.meta.get("artifact_sha256"),
        }


def parse_models(models_raw: dict | None, strategy_dir: str = "") -> list[ModelSpec]:
    """解析策略 YAML 的 models 节为 ModelSpec 列表（引擎路径，meta 必需）。"""
    if not models_raw:
        return []
    if not isinstance(models_raw, dict):
        raise ValueError(f"models 必须是 mapping: {models_raw!r}")
    return [
        ModelSpec.from_dict(name, d, strategy_dir)
        for name, d in models_raw.items()
    ]
