// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import "./AccessController.sol";
import "./Treasury.sol";
import "./adapters/IDexAdapter.sol";
import "./libs/StepCodec.sol";

contract ArbExecutor is ReentrancyGuard {
    using SafeERC20 for IERC20;

    error NotExecutor();
    error DeadlineExpired();
    error AdapterNotAllowed();
    error NoProfit();

    event Executed(bytes32 indexed execHash, address indexed profitToken, uint256 profit, uint256 steps);
    event AdapterAllowed(address indexed adapter, bool allowed);

    AccessController public immutable access;
    Treasury public immutable treasury ;

    mapping(address => bool) public adapterAllowed;

    constructor(address accessController, address treasury_) {
        access = AccessController(accessController);
        treasury = Treasury (payable(treasury_));
    }

    struct Execution {
        address profitToken;   // в чём считаем профит (USDC/WETH)
        uint256 minProfit;     // минимальный профит
        uint256 deadline;      // дедлайн
        bytes plan;            // abi.encode(StepCodec.Step[])
    }

    modifier onlyExecutor() {
        if (!access.hasRole(access.EXECUTOR_ROLE(), msg.sender)) revert NotExecutor();
        _;
    }

    function setAdapterAllowed(address adapter, bool allowed) external {
        // админ = DEFAULT_ADMIN_ROLE в AccessController
        require(access.hasRole(access.ADMIN_ROLE(), msg.sender), "NOT_ADMIN");
        adapterAllowed[adapter] = allowed;
        emit AdapterAllowed(adapter, allowed);
    }

    function execute(Execution calldata e) external nonReentrant onlyExecutor {
        if (block.timestamp > e.deadline) revert DeadlineExpired();

        uint256 beforeBal = IERC20(e.profitToken).balanceOf(address(this));

        StepCodec.Step[] memory steps = StepCodec.decodePlan(e.plan);

        for (uint256 i = 0; i < steps.length; i++) {
            if (!adapterAllowed[steps[i].adapter]) revert AdapterNotAllowed();
            IDexAdapter(steps[i].adapter).swap(steps[i].data);
        }

        uint256 afterBal = IERC20(e.profitToken).balanceOf(address(this));
        uint256 profit = afterBal > beforeBal ? (afterBal - beforeBal) : 0;

        if (profit < e.minProfit) revert NoProfit();

        // отправим профит в treasury (чисто, удобно)
        if (profit > 0) {
            IERC20(e.profitToken).safeTransfer(address(treasury), profit);
        }

        bytes32 execHash = keccak256(abi.encode(msg.sender, e.profitToken, e.minProfit, e.deadline, e.plan, beforeBal, afterBal));
        emit Executed(execHash, e.profitToken, profit, steps.length);
    }

    // пополнение — просто transfer на адрес контракта,
    // но можно оставить “спасалку” для админа
    function rescueToken(address token, address to, uint256 amount) external {
        require(access.hasRole(access.ADMIN_ROLE(), msg.sender), "NOT_ADMIN");
        IERC20(token).safeTransfer(to, amount);
    }

    receive() external payable {}
}
