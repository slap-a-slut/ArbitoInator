// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title StepCodec
 * @dev Library for encoding and decoding arbitrage execution steps
 * @notice Handles serialization of DEX swap steps for efficient execution
 */
library StepCodec {
    /**
     * @dev Struct representing a single swap step
     */
    struct Step {
        address adapter;    // DEX adapter contract address
        bytes data;         // Calldata for the swap
    }
    
    /**
     * @dev Encode multiple steps into a single bytes array
     * @param steps Array of Step structs
     * @return encoded Concatenated bytes representation
     */
    function encodePlan(Step[] memory steps) internal pure returns (bytes memory encoded) {
        if (steps.length == 0) return new bytes(0);
        
        uint256 totalSize = 0;
        for (uint256 i = 0; i < steps.length; i++) {
            totalSize += 32 + 32 + steps[i].data.length; // adapter + data offset + data
        }
        
        encoded = new bytes(totalSize);
        uint256 offset = 0;
        
        for (uint256 i = 0; i < steps.length; i++) {
            // Write adapter address
            address adapter = steps[i].adapter;
            assembly {
                mstore(add(add(encoded, 32), offset), adapter)
            }
            offset += 32;
            
            // Write data offset (relative to current position)
            uint256 dataOffset = offset + 32;
            assembly {
                mstore(add(add(encoded, 32), offset), dataOffset)
            }
            offset += 32;
            
            // Write data length and actual data
            uint256 dataLength = steps[i].data.length;
            assembly {
                mstore(add(add(encoded, 32), offset), dataLength)
            }
            offset += 32;
            
            // Copy data
            bytes memory stepData = steps[i].data;
            for (uint256 j = 0; j < dataLength; j++) {
                encoded[offset] = stepData[j];
                offset++;
            }
        }
        
        return encoded;
    }
    
    /**
     * @dev Decode bytes array back into Step array
     * @param encoded Encoded plan bytes
     * @return steps Array of Step structs
     */
    function decodePlan(bytes memory encoded) internal pure returns (Step[] memory steps) {
        if (encoded.length == 0) return new Step[](0);
        
        // First pass: count steps by scanning for adapter addresses
        uint256 stepCount = 0;
        uint256 offset = 0;
        
        while (offset < encoded.length) {
            if (offset + 64 > encoded.length) break; // Not enough for adapter + offset
            stepCount++;
            
            // Read data offset
            uint256 dataOffset;
            assembly {
                dataOffset := mload(add(add(encoded, 32), add(offset, 32)))
            }
            
            // Read data length
            if (dataOffset > encoded.length) break;
            uint256 dataLength;
            assembly {
                dataLength := mload(add(add(encoded, 32), dataOffset))
            }
            
            offset = dataOffset + 32 + dataLength; // Move to next step
        }
        
        steps = new Step[](stepCount);
        offset = 0;
        
        for (uint256 i = 0; i < stepCount; i++) {
            // Read adapter address
            address adapter;
            assembly {
                adapter := mload(add(add(encoded, 32), offset))
            }
            
            // Read data offset
            uint256 dataOffset;
            assembly {
                dataOffset := mload(add(add(encoded, 32), add(offset, 32)))
            }
            
            // Read data length
            uint256 dataLength;
            assembly {
                dataLength := mload(add(add(encoded, 32), dataOffset))
            }
            
            // Extract data
            bytes memory data = new bytes(dataLength);
            uint256 dataStart = dataOffset + 32;
            for (uint256 j = 0; j < dataLength; j++) {
                if (dataStart + j < encoded.length) {
                    data[j] = encoded[dataStart + j];
                }
            }
            
            steps[i] = Step({
                adapter: adapter,
                data: data
            });
            
            offset = dataStart + dataLength;
        }
        
        return steps;
    }
    
    /**
     * @dev Create a single step
     * @param adapter DEX adapter address
     * @param data Swap calldata
     * @return step Step struct
     */
    function createStep(address adapter, bytes memory data) internal pure returns (Step memory step) {
        return Step({
            adapter: adapter,
            data: data
        });
    }
    
    /**
     * @dev Get the number of steps in encoded plan (simplified version)
     * @param encoded Encoded plan bytes
     * @return count Number of steps
     */
    function getStepCount(bytes memory encoded) internal pure returns (uint256 count) {
        if (encoded.length == 0) return 0;
        
        uint256 offset = 0;
        while (offset < encoded.length) {
            if (offset + 64 > encoded.length) break;
            count++;
            
            uint256 dataOffset;
            assembly {
                dataOffset := mload(add(add(encoded, 32), add(offset, 32)))
            }
            
            if (dataOffset > encoded.length) break;
            
            uint256 dataLength;
            assembly {
                dataLength := mload(add(add(encoded, 32), dataOffset))
            }
            
            offset = dataOffset + 32 + dataLength;
        }
        
        return count;
    }
}