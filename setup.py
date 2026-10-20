from setuptools import setup, find_packages

setup(
    name="refactorer-tool",
    version="0.1.0",
    description="Automated refactoring tool to address architectural smells",
    python_requires=">=3.11",
    packages=find_packages(),
    install_requires=[
      "code-quality-analyzer @ git+https://github.com/jbpextra/python_smells_detector.git@add-participant-to-architectural-smells#egg=code-quality-analyzer",
      "openai>=1.108.1",
      "PyYAML>=6.0",
      "code-bert-score",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0",
            "httpx>=0.23,<1",
            "black>=23.9.1",
        ],
    },
    entry_points={
        "console_scripts": [
            "apply-refactoring = scripts.apply_refactoring:main",
            "correctness-filter = scripts.correctness_filter:main",
        ],
    },
)
