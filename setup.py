from setuptools import setup, find_packages

setup(
    name="graphthink",
    version="0.1.0",
    description="Sovereign graph memory for AI agents. Self-hosted. Zero API costs.",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="Sai Sharan",
    author_email="saisharan.ssr@gmail.com",
    url="https://github.com/saisharan81/graphthink",
    packages=find_packages(),
    python_requires=">=3.9",
    project_urls={
        "Source": "https://github.com/saisharan81/graphthink",
        "Bug Tracker": "https://github.com/saisharan81/graphthink/issues",
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
    ],
)
