def decompose_single(
        self,
        fund_code: str,
        end_date: date,
        window_days: int = 60,
        sector_codes: Optional[List[str]] = None,
        non_negative: bool = True,
        sum_to_one: bool = False,
    ) -> DecompositionResult:
        """
        单期风格分解（单日截面）
        """
        if sector_codes is None:
            sector_codes = list(self.sector_returns.keys())

        fund_ret, sector_ret = self._align_data(fund_code, sector_codes, end_date, window_days)

        if fund_ret.empty or len(sector_ret.columns) < 2:
            _logger.warning("基金 [%s] 于 %s 数据不足，无法分解", fund_code, end_date)
            return self._skipped_result(fund_code, end_date, window_days)

        y = fund_ret["fund_return"].values
        X = sector_ret.values
        n_sectors = X.shape[1]

        # 数值闸门：进优化器的 y / X 必须全有限。
        # pct_change 遇到 0 净值会产 inf，交集日期也可能残留缺失值；这类脏值
        # 流进 SLSQP 后只会得到一句与真实原因无关的「Inequality constraints
        # incompatible」，然后被当成「优化失败」吞掉 —— 根因完全看不出来。
        if not (np.isfinite(y).all() and np.isfinite(X).all()):
            _logger.warning(
                "基金 [%s] 于 %s 对齐后的收益含非有限值（NaN/inf），放弃该时点",
                fund_code, end_date,
            )
            return self._skipped_result(fund_code, end_date, window_days)

        # 约束条件
        if non_negative:
            bounds = Bounds(0, 1)
        else:
            bounds = Bounds(-1, 1)

        constraints = []
        if sum_to_one:
            constraints.append(LinearConstraint(np.ones(n_sectors), lb=0.95, ub=1.0))

        # 目标函数：最小化残差平方和
        def objective(w):
            residuals = y - X @ w
            return np.sum(residuals ** 2)

        # 初始猜测：等权
        w0 = np.ones(n_sectors) / n_sectors

        result = minimize(
            objective, w0,
            bounds=bounds,
            constraints=constraints,
            method="SLSQP",
            options={"maxiter": 1000, "ftol": 1e-9}
        )

        if not result.success or not np.isfinite(result.fun) \
                or not np.isfinite(result.x).all():
            # 优化失败 = 这一期没算出来，**不拿等权 w0 顶替**：
            # 等权会让「算不出来」伪装成「持仓没变」，信号层读到一片假稳定，
            # 比少一个时点更糟（滚动序列少一点，上层会明说点数不足）。
            _logger.warning(
                "基金 [%s] 于 %s 分解优化失败，跳过该时点: %s",
                fund_code, end_date, result.message,
            )
            return self._skipped_result(fund_code, end_date, window_days)

        weights = result.x
        exposures = {code: float(w) for code, w in zip(sector_ret.columns, weights)}
        total_exposure = sum(exposures.values())
        cash_exposure = max(0, 1 - total_exposure)

        # 计算统计指标
        y_pred = X @ weights
        residuals = y - y_pred
        alpha = np.mean(residuals) * 252  # 年化
        residual_vol = np.std(residuals) * np.sqrt(252)
        ss_tot = float(np.sum((y - np.mean(y)) ** 2))
        if not np.isfinite(ss_tot) or ss_tot <= 0:
            # 窗口内基金收益恒定 -> R² 数学上无定义。
            # 记 0 而不是留 NaN：NaN 会绕过上层「R²<0.5 不可信」的告警
            # （nan < 0.5 恒为 False），还会把 summary.avg_r_squared 变成 NaN
            # 直接污染 JSON。0 既不假装拟合好，也必然触发不可信告警。
            _logger.warning("基金 [%s] 于 %s 窗口内收益无波动，R² 无定义（按 0 计）",
                            fund_code, end_date)
            r_squared = 0.0
        else:
            r_squared = 1 - float(np.sum(residuals ** 2)) / ss_tot
            r_squared = float(r_squared) if np.isfinite(r_squared) else 0.0

        # 获取行业层级
        level = 1
        if self.industry_mapping_df is not None:
            levels = self.industry_mapping_df.set_index("index_code")["level"]
            level = int(levels[sector_ret.columns[0]]) if sector_ret.columns[0] in levels.index else 1

        return DecompositionResult(
            fund_code=fund_code,
            trade_date=end_date,
            window_days=window_days,
            level=level,
            exposures=exposures,
            r_squared=float(r_squared),
            alpha=float(alpha),
            residual_vol=float(residual_vol),
            n_sectors=n_sectors,
            cash_exposure=float(cash_exposure),
        )